"""Tests for the street + disposition dedup engine.

The pure rule logic (toolkit.dedup_engine) is tested directly — no DB. The
orchestration (scripts.dedup_engine.run_engine) is tested against a scripted
fake connection with injected classify/compare callables, so the stage flow,
caps, and merge/queue dispatch are verified without a real DB or LLM.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from toolkit.dedup_engine import (
    ADDRESS_AREA_GUARD_PCT,
    CANDIDATE_AREA_MAX_PCT,
    RENDER_SCORE_EXCLUDE_MIN,
    CosineBands,
    ListingKey,
    MatchProfile,
    category_main_compatible,
    classify_byt_geo_pair,
    classify_geo_pair,
    classify_pair,
    decide_phash_fastpath,
    decide_visual_dismiss,
    disposition_class,
    disposition_compatible,
    distinctive_rooms_for,
    prioritized_group_pairs,
    geo_category_bucket,
    geo_cell_key,
    normalize_street,
    phash_excluded_tags_for,
    phash_render_exclude_for,
    profile_for,
    room_priority_for,
    rooms_in_priority,
    route_by_cosine,
    street_group_keys,
    verdict_is_merge,
)
from toolkit.dedup_engine import (
    TAG_PRIORITY_FAMILIES,
    default_priority_for_family,
    normalize_priority,
)
from toolkit.room_taxonomy import (
    DISTINCTIVE_ROOMS,
    HOUSE_PRIORITY,
    INTERIOR_PRIORITY,
    LAND_PRIORITY,
)


def _dismissed_pairs(conn: Any) -> set[tuple[int, int]]:
    """Property pairs the run resolved to 'dismissed' (via _resolve_candidates)."""
    out: set[tuple[int, int]] = set()
    for _s, params in conn.resolved_status("dismissed"):
        _status, los, his = params
        out.update(zip(los, his))
    return out


def _key(
    sid: int, *, pid: int | None = None, source: str = "sreality",
    street: str = "id:42", disp: str = "2+kk", hn: str | None = "10",
    floor: int | None = 3, area: float | None = 60.0,
    description: str | None = None,
    category_type: str | None = "prodej", category_main: str | None = "byt",
    street_id: int | None = None, lid: int | None = None,
) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid if pid is not None else sid,
        source=source, street_key=street, disposition=disp,
        house_number=hn, floor=floor, area_m2=area, description=description,
        category_type=category_type, category_main=category_main,
        street_id=street_id, listing_id=lid if lid is not None else sid + 100_000,
    )


# --- normalize_street -------------------------------------------------------

def test_normalize_street_prefers_street_id() -> None:
    assert normalize_street("Nádražní", 42) == "id:42"


def test_normalize_street_falls_back_to_name_diacritics_stripped() -> None:
    assert normalize_street("Nádražní 12", None) == "name:nadrazni"


def test_normalize_street_none_when_empty_or_negative_id() -> None:
    assert normalize_street(None, None) is None
    assert normalize_street("", None) is None
    assert normalize_street("Hlavní", -1) == "name:hlavni"  # -1 sentinel -> name


def test_normalize_street_strips_street_words_and_house_numbers() -> None:
    # bazos "ul. Hlavní 12" and sreality "Hlavní" are one street.
    assert (
        normalize_street("ul. Hlavní 12", None)
        == normalize_street("Hlavní", None)
        == normalize_street("hlavni 123/4a", None)
        == "name:hlavni"
    )
    assert normalize_street("ulice Dlouhá", None) == "name:dlouha"
    assert normalize_street("nám. Míru", None) == "name:miru"
    assert normalize_street("náměstí Míru", None) == "name:miru"
    assert normalize_street("tř. Svobody", None) == "name:svobody"
    assert normalize_street("třída Svobody", None) == "name:svobody"
    assert normalize_street("Vinohradská třída 5", None) == "name:vinohradska"
    assert normalize_street("Sídliště Osvobození 650/29", None) == "name:osvobozeni"
    assert normalize_street("nábřeží Závodu míru", None) == "name:zavodu miru"


def test_normalize_street_words_only_strip_as_whole_tokens() -> None:
    # Names merely STARTING like a street word stay intact.
    assert normalize_street("Třebízského", None) == "name:trebizskeho"
    assert normalize_street("Trnková 12", None) == "name:trnkova"
    assert normalize_street("Brno", None) == "name:brno"
    # Date-streets keep their leading ordinal (only TRAILING numbers strip).
    assert normalize_street("28. října 12", None) == "name:28. rijna"


def test_normalize_street_never_empties_the_key() -> None:
    assert normalize_street("ulice", None) == "name:ulice"
    assert normalize_street("Náměstí", None) == "name:namesti"
    assert normalize_street("123", None) == "name:123"


def test_street_group_keys_dual_keys_id_and_name() -> None:
    # NAME key is obec-scoped (default obec_id=None when unknown).
    assert street_group_keys("Hlavní", 42) == ("id:42", "name:None:hlavni")
    assert street_group_keys(None, 42) == ("id:42",)
    assert street_group_keys("ul. Hlavní 12", None) == ("name:None:hlavni",)
    assert street_group_keys(None, None) == ()
    assert street_group_keys("", -1) == ()


def test_street_group_keys_scopes_name_by_obec() -> None:
    # Same street name in two municipalities → DIFFERENT name groups (so a
    # common name like "Žižkova" no longer lumps every town into one oversized,
    # skipped group). The id: key stays global (a street_id is one street).
    a = street_group_keys("Žižkova", None, 5001)
    b = street_group_keys("Žižkova", None, 5002)
    assert a == ("name:5001:zizkova",)
    assert b == ("name:5002:zizkova",)
    assert a != b
    # A street_id-bearing row still dual-keys into its obec-scoped name group,
    # so it can meet a name-only HTML-portal row in the same town.
    assert street_group_keys("Žižkova", 98996, 5001) == ("id:98996", "name:5001:zizkova")


# --- disposition compatibility ----------------------------------------------

def test_disposition_loose_equivalence() -> None:
    assert disposition_compatible("2+kk", "2+1")
    assert disposition_compatible("2+kk", "2+kk")
    assert not disposition_compatible("2+kk", "3+kk")
    assert not disposition_compatible(None, "2+kk")


# --- classify_pair: exact address (rule B RETIRED -> a strong CANDIDATE) -----

def test_exact_address_is_candidate_not_auto_merge() -> None:
    # Rule B retired (6.7% false merges): exact address is now a rule-C CANDIDATE that flows
    # through pHash/visual, with the `address_exact` reason kept for provenance.
    d = classify_pair(_key(1, source="sreality"), _key(2, source="bazos"))
    assert d.action == "candidate"
    assert d.reason == "address_exact"


def test_exact_address_candidate_same_source() -> None:
    d = classify_pair(_key(1, source="sreality"), _key(2, source="sreality"))
    assert d.action == "candidate"
    assert d.reason == "address_exact"


def test_exact_address_area_guard_demotes_to_candidate() -> None:
    # same street+no+disp+floor, areas 60 vs 65 (7.7%: >5% guard, <10% byt reject)
    # -> visual candidate (area_guard), not a hard area_contradiction reject.
    d = classify_pair(_key(1, area=60.0), _key(2, area=65.0))
    assert d.action == "candidate"
    assert d.reason == "area_guard"


def test_exact_address_within_area_guard_is_candidate() -> None:
    d = classify_pair(_key(1, area=60.0), _key(2, area=62.0))  # ~3% < 5%
    assert d.action == "candidate"
    assert d.reason == "address_exact"


# --- classify_pair: rule C (candidate + disqualifiers) ----------------------

def test_same_street_disposition_no_house_number_is_candidate() -> None:
    d = classify_pair(_key(1, hn=None), _key(2, hn=None))
    assert d.action == "candidate"
    assert d.reason is None


def test_floor_contradiction_rejects() -> None:
    d = classify_pair(_key(1, floor=2), _key(2, floor=5))
    assert d.action == "reject"
    assert d.detail == "floor_contradiction"


def test_floor_off_by_one_is_candidate_not_reject() -> None:
    # idnes counts the ground floor as 0 (patro), sreality as 1 (NP): the SAME
    # flat reads one floor apart across portals. A gap of 1 is convention noise,
    # not a contradiction -> visual candidate, never a hard reject.
    d = classify_pair(_key(1, hn=None, floor=2), _key(2, hn=None, floor=3))
    assert d.action == "candidate"
    assert d.detail != "floor_contradiction"


def test_floor_gap_of_two_still_rejects() -> None:
    d = classify_pair(_key(1, hn=None, floor=2), _key(2, hn=None, floor=4))
    assert d.action == "reject"
    assert d.detail == "floor_contradiction"


def test_area_contradiction_rejects_large_gap() -> None:
    d = classify_pair(_key(1, hn=None, area=50.0), _key(2, hn=None, area=80.0))
    assert d.action == "reject"
    assert d.detail == "area_contradiction"


def test_byt_area_gap_over_10pct_rejects() -> None:
    # The "Rezidence Na Bradle" fix: byt units one area-band apart (73 vs 87 = 16%,
    # 87 vs 99 = 12%) are now a hard reject before pHash, so shared renders can't
    # chain-merge them. 60 vs 70 = 14.3% > 10%.
    d = classify_pair(_key(1, hn=None, area=60.0), _key(2, hn=None, area=70.0))
    assert d.action == "reject"
    assert d.detail == "area_contradiction"


def test_area_gate_unified_10pct_across_categories() -> None:
    # The candidate area gap is unified at 10% for every category (operator decision).
    for fam in ("byt", "dum", "pozemek", "komercni", "ostatni"):
        assert profile_for(fam).candidate_area_max_pct == CANDIDATE_AREA_MAX_PCT == 0.10, fam


def test_house_number_contradiction_rejects() -> None:
    d = classify_pair(_key(1, hn="10", floor=None), _key(2, hn="12", floor=None))
    assert d.action == "reject"
    assert d.detail == "house_number_contradiction"


def test_unit_marker_contradiction_rejects_different_plots() -> None:
    # the real Kostelec development: pozemek č.4 vs č.3, identical otherwise
    a = _key(1, hn=None, description="dům 4+kk, pozemek č.4 o velikosti 479 m²")
    b = _key(2, hn=None, description="dům 4+kk, pozemek č.3 o velikosti 484 m²")
    d = classify_pair(a, b)
    assert d.action == "reject"
    assert d.detail == "unit_marker_contradiction"


def test_unit_marker_contradiction_house_and_flat_variants() -> None:
    assert classify_pair(
        _key(1, hn=None, description="dům 3A na klíč"),
        _key(2, hn=None, description="dům 5C na klíč"),
    ).detail == "unit_marker_contradiction"
    assert classify_pair(
        _key(1, hn=None, description="prodej byt 42 v centru"),
        _key(2, hn=None, description="prodej byt 45 v centru"),
    ).detail == "unit_marker_contradiction"


def test_same_unit_marker_does_not_block() -> None:
    # identical descriptions (same unit, or no unit token) → not a contradiction
    a = _key(1, hn=None, description="komerční nemovitost v centru, jednotka č. 5")
    b = _key(2, hn=None, description="komerční nemovitost v centru, jednotka č. 5")
    assert classify_pair(a, b).action == "candidate"
    # unit keyword present on only one side → no contradiction
    c = _key(3, hn=None, description="pozemek č.4")
    e = _key(4, hn=None, description="hezký dům u lesa")
    assert classify_pair(c, e).action == "candidate"


def test_unit_marker_building_letter_labels_reject() -> None:
    # "Budova A" vs "Budova B" etc. — the development-container letter labels.
    for kw in ("Budova", "Blok", "Vchod", "Etapa"):
        d = classify_pair(
            _key(1, hn=None, description=f"{kw} A, pěkný byt 2+kk"),
            _key(2, hn=None, description=f"{kw} B, pěkný byt 2+kk"),
        )
        assert d.detail == "unit_marker_contradiction", kw


def test_unit_marker_numeric_containers_reject() -> None:
    assert classify_pair(
        _key(1, hn=None, description="objekt č. 3 v rezidenci"),
        _key(2, hn=None, description="objekt č. 5 v rezidenci"),
    ).detail == "unit_marker_contradiction"


def test_unit_marker_lowercase_conjunction_not_a_label() -> None:
    # The Czech conjunction "a"/"i" must never read as a building letter label.
    assert classify_pair(
        _key(1, hn=None, description="prodej domu a pozemku, byt 2+kk"),
        _key(2, hn=None, description="prodej domu i zahrady, byt 2+kk"),
    ).action == "candidate"


def test_street_mismatch_rejects() -> None:
    d = classify_pair(_key(1, street="id:1"), _key(2, street="id:2"))
    assert d.action == "reject"
    assert d.detail == "street_mismatch"


def test_street_id_contradiction_rejects_same_name_different_street() -> None:
    # Dual-keying puts two same-named streets from different towns in one name
    # group; differing canonical ids are a hard reject.
    d = classify_pair(
        _key(1, street="name:hlavni", street_id=42),
        _key(2, street="name:hlavni", street_id=99),
    )
    assert d.action == "reject"
    assert d.detail == "street_id_contradiction"
    # A NULL id (bazos) never contradicts a known one (sreality) -> not rejected (candidate).
    assert classify_pair(
        _key(1, street="name:hlavni", street_id=42),
        _key(2, street="name:hlavni", street_id=None, source="bazos"),
    ).action == "candidate"


def test_disposition_mismatch_rejects() -> None:
    d = classify_pair(_key(1, disp="2+kk", hn=None), _key(2, disp="3+kk", hn=None))
    assert d.action == "reject"
    assert d.detail == "disposition_mismatch"


def test_category_type_contradiction_rejects_sale_vs_rent() -> None:
    # The reported bug: a sale and a rental on the same street + disposition.
    d = classify_pair(
        _key(1, category_type="prodej"),
        _key(2, category_type="pronajem"),
    )
    assert d.action == "reject"
    assert d.detail == "category_type_contradiction"


def test_category_type_contradiction_drazba_vs_prodej() -> None:
    # Distinct offerings even though both are sale-like.
    assert classify_pair(
        _key(1, category_type="drazba"),
        _key(2, category_type="prodej"),
    ).detail == "category_type_contradiction"


def test_category_main_contradiction_byt_vs_dum() -> None:
    assert classify_pair(
        _key(1, category_main="byt"),
        _key(2, category_main="dum"),
    ).detail == "category_main_contradiction"


def test_cross_type_dum_komercni_is_not_a_contradiction() -> None:
    # Operator policy: the ONE sanctioned cross-type. A building listed as a house on
    # one portal and commercial on another is the same real-world property — it must
    # NOT reject on category_main, it falls through to merge like a same-category pair.
    d = classify_pair(
        _key(1, category_main="dum", disp=None),
        _key(2, category_main="komercni", disp=None),
    )
    assert d.action == "candidate"
    assert d.detail != "category_main_contradiction"
    # symmetric — order must not matter
    assert classify_pair(
        _key(1, category_main="komercni", disp=None),
        _key(2, category_main="dum", disp=None),
    ).action == "candidate"


def test_category_main_compatible_helper() -> None:
    # Equal, or either side unknown (NULL), is always compatible.
    assert category_main_compatible("byt", "byt") is True
    assert category_main_compatible(None, "dum") is True
    assert category_main_compatible("komercni", None) is True
    # The one sanctioned cross-type, both directions.
    assert category_main_compatible("dum", "komercni") is True
    assert category_main_compatible("komercni", "dum") is True
    # Every other mismatch stays a contradiction.
    assert category_main_compatible("byt", "dum") is False
    assert category_main_compatible("byt", "komercni") is False
    assert category_main_compatible("dum", "pozemek") is False
    assert category_main_compatible("pozemek", "komercni") is False


def test_category_null_does_not_contradict() -> None:
    # A missing category is unknown, not a conflict — falls through to a candidate.
    assert classify_pair(
        _key(1, category_type=None),
        _key(2, category_type="pronajem"),
    ).action == "candidate"


def test_already_same_property_rejects() -> None:
    d = classify_pair(_key(1, pid=99), _key(2, pid=99))
    assert d.action == "reject"
    assert d.detail == "already_merged"


def test_missing_floor_on_one_side_is_candidate_not_merge() -> None:
    # no exact-address merge without floor on both; not a contradiction either
    d = classify_pair(_key(1, floor=None), _key(2, floor=3))
    assert d.action == "candidate"


# --- MatchProfile: per-category matching policy -----------------------------

def test_profile_for_known_families() -> None:
    assert profile_for("byt").family == "byt"
    assert profile_for("dum").family == "dum"
    assert profile_for("pozemek").family == "pozemek"
    assert profile_for("komercni").family == "komercni"
    assert profile_for("ostatni").family == "ostatni"


def test_profile_for_null_or_unknown_is_byt() -> None:
    # A row with no/unknown category must behave exactly as the legacy engine.
    assert profile_for(None) is profile_for("byt")
    assert profile_for("") is profile_for("byt")
    assert profile_for("garaz").family == "byt"


def test_byt_profile_constants() -> None:
    # The byt profile: disposition mandatory, street-blocked (not geo), never
    # geo-auto-merges. The candidate area gap is unified at 10% across all categories.
    byt = profile_for("byt")
    assert byt.disposition_required is True
    assert byt.address_area_guard_pct == ADDRESS_AREA_GUARD_PCT
    assert byt.candidate_area_max_pct == CANDIDATE_AREA_MAX_PCT == 0.10
    assert byt.geo_blocked is False
    assert byt.geo_auto_merge_allowed is False


def test_single_dwelling_profiles_drop_disposition_and_geo_block() -> None:
    # Houses/land/commercial have no usable disposition → it is not a matching key,
    # and they route through the (P1) geo-cell block instead of street-only.
    for fam in ("dum", "pozemek", "komercni", "ostatni"):
        p = profile_for(fam)
        assert p.disposition_required is False, fam
        assert p.geo_blocked is True, fam


def test_only_houses_geo_auto_merge() -> None:
    # Operator policy: only dum carries the geo-auto-merge flag (coord+area+price,
    # validated 83.5% same-price); land/commercial/other are queue-only (weaker
    # signal). NOTE the orchestrator maps the geo auto_merge → candidate, so today
    # the flag only differentiates the queued reason tag (see MatchProfile docstring).
    assert profile_for("dum").geo_auto_merge_allowed is True
    for fam in ("pozemek", "komercni", "ostatni"):
        assert profile_for(fam).geo_auto_merge_allowed is False, fam


def test_geo_blocked_is_derived_from_publication_geo_families() -> None:
    # Single-sourcing: the qualification layer's category list (publication.GEO_FAMILIES)
    # IS what makes a profile geo-blocked — no hand-kept boolean to drift.
    from toolkit.publication import GEO_FAMILIES

    assert GEO_FAMILIES == ("dum", "pozemek", "komercni", "ostatni")
    for fam in ("byt", *GEO_FAMILIES):
        assert profile_for(fam).geo_blocked is (fam in GEO_FAMILIES), fam


def test_match_profile_is_frozen() -> None:
    with pytest.raises(Exception):
        profile_for("byt").disposition_required = False  # type: ignore[misc]


def test_classify_pair_still_requires_disposition_for_byt() -> None:
    # Dark-landing guard: the profile path must NOT relax disposition for apartments
    # (profile_for('byt').disposition_required is True), so a missing disposition on
    # one side still rejects exactly as the legacy engine did.
    d = classify_pair(_key(1, disp="2+kk"), _key(2, disp=None))
    assert d.action == "reject"
    assert d.detail == "disposition_mismatch"


def test_match_profile_is_a_dataclass_type() -> None:
    assert isinstance(profile_for("byt"), MatchProfile)


# --- per-family image comparison priority + distinctive override ------------

def test_room_priority_for_byt_leads_with_interior() -> None:
    # Apartments compare interior rooms (wet rooms first) — the legacy order, unchanged.
    pr = room_priority_for("byt")
    assert pr == INTERIOR_PRIORITY
    assert pr[0] == "kitchen"


def test_room_priority_for_house_and_commercial_lead_with_facade() -> None:
    # A house/commercial building's identity is its FACADE, so it leads stop-at-first-High.
    for fam in ("dum", "komercni", "ostatni"):
        pr = room_priority_for(fam)
        assert pr == HOUSE_PRIORITY, fam
        assert pr[0] == "exterior_facade", fam


def test_room_priority_for_land_leads_with_site_plan() -> None:
    # A plot's identity is its SITE PLAN (the development guard reads it).
    pr = room_priority_for("pozemek")
    assert pr == LAND_PRIORITY
    assert pr[0] == "site_plan"


def test_room_priority_for_null_or_unknown_is_byt_order() -> None:
    # Unknown/NULL category behaves as the legacy byt engine.
    assert room_priority_for(None) == INTERIOR_PRIORITY
    assert room_priority_for("garaz") == INTERIOR_PRIORITY


def test_normalize_priority_keeps_order_drops_unknown_completes_from_default() -> None:
    default = ("a", "b", "c", "d")
    # operator order honoured; unknown 'z' dropped; duplicate ignored; missing 'd' appended.
    assert normalize_priority(["c", "a", "z", "a"], default) == ("c", "a", "b", "d")
    # empty / all-unknown → exactly the default (never drops a room).
    assert normalize_priority([], default) == default
    assert normalize_priority(["z", "y"], default) == default


def test_room_priority_for_honours_overrides_per_family() -> None:
    # A house override that leads with kitchen instead of facade, completed from HOUSE default.
    ov = {"dum": ["kitchen", "exterior_facade"]}
    pr = room_priority_for("dum", ov)
    assert pr[0] == "kitchen" and pr[1] == "exterior_facade"
    assert set(pr) == set(HOUSE_PRIORITY)  # nothing dropped
    # a family without an override is untouched.
    assert room_priority_for("byt", ov) == INTERIOR_PRIORITY
    # an empty override list falls back to the coded default.
    assert room_priority_for("dum", {"dum": []}) == HOUSE_PRIORITY


def test_default_priority_for_family_matches_room_priority_for() -> None:
    assert default_priority_for_family("byt") == INTERIOR_PRIORITY
    assert default_priority_for_family("pozemek") == LAND_PRIORITY
    for fam in ("dum", "komercni", "ostatni"):
        assert default_priority_for_family(fam) == HOUSE_PRIORITY
    assert set(TAG_PRIORITY_FAMILIES) == {"byt", "dum", "komercni", "ostatni", "pozemek"}


def test_distinctive_rooms_for_is_byt_only() -> None:
    # The single-pHash-match override (count-of-1) is a byt-only signal: a wet room is
    # unit-specific, but a house's facade / a plot's site plan is shared across a
    # development, so non-apartments get an EMPTY set (require the >=2-match count).
    assert distinctive_rooms_for("byt") == DISTINCTIVE_ROOMS
    assert distinctive_rooms_for(None) == DISTINCTIVE_ROOMS
    for fam in ("dum", "pozemek", "komercni", "ostatni"):
        assert distinctive_rooms_for(fam) == frozenset(), fam


# --- geo path: single-dwelling families -------------------------------------

def _gk(
    sid: int, pid: int, *, source: str = "sreality", cat: str = "dum",
    ct: str = "prodej", area: float | None = 120.0, price: int | None = 5_950_000,
    hn: str | None = None, lat: float = 50.10064, lng: float = 14.53742,
    desc: str | None = None, street_eligible: bool = False, lid: int | None = None,
) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid, source=source, street_key="geo:cell",
        disposition="", house_number=hn, floor=None, area_m2=area, description=desc,
        category_type=ct, category_main=cat, street_id=None, lat=lat, lng=lng,
        price_czk=price, street_eligible=street_eligible,
        listing_id=lid if lid is not None else sid + 100_000,
    )


def test_geo_cell_key_format_and_scoping() -> None:
    # dum and komercni collapse to one category bucket so the cross-type co-locates.
    assert geo_cell_key(5001, 50.10064, 14.53742, "dum", "prodej") == "geo:5001:50.1006:14.5374:dum|komercni:prodej"
    base = geo_cell_key(5001, 50.10064, 14.53742, "dum", "prodej")
    # different obec / offering → different cell (no cross-bucket pairing)
    assert geo_cell_key(5002, 50.10064, 14.53742, "dum", "prodej") != base
    assert geo_cell_key(5001, 50.10064, 14.53742, "dum", "pronajem") != base
    # dum and komercni SHARE a cell (the sanctioned cross-type); pozemek stays separate.
    assert geo_cell_key(5001, 50.10064, 14.53742, "komercni", "prodej") == base
    assert geo_cell_key(5001, 50.10064, 14.53742, "pozemek", "prodej") != base


def test_geo_category_bucket_collapses_dum_komercni() -> None:
    assert geo_category_bucket("dum") == geo_category_bucket("komercni") == "dum|komercni"
    assert geo_category_bucket("pozemek") == "pozemek"
    assert geo_category_bucket("byt") == "byt"
    assert geo_category_bucket(None) is None


def test_geo_cell_key_none_when_coord_or_obec_missing() -> None:
    assert geo_cell_key(None, 50.1, 14.5, "dum", "prodej") is None
    assert geo_cell_key(5001, None, 14.5, "dum", "prodej") is None
    assert geo_cell_key(5001, 50.1, None, "dum", "prodej") is None


def test_classify_geo_dum_strong_auto_merges() -> None:
    # identical coord + area + price across two portals → strong; dum may auto-merge.
    d = classify_geo_pair(_gk(1, 101, source="sreality"), _gk(2, 102, source="idnes"), profile_for("dum"))
    assert d.action == "auto_merge"
    assert d.reason == "geo_exact"


def test_classify_geo_strong_via_house_number_even_if_price_differs() -> None:
    d = classify_geo_pair(
        _gk(1, 101, price=5_000_000, hn="12"),
        _gk(2, 102, price=9_000_000, hn="12"),
        profile_for("dum"),
    )
    assert d.action == "auto_merge" and d.reason == "geo_exact"


def test_classify_geo_land_strong_is_candidate_never_auto_merge() -> None:
    # Same strong signal, but land's profile forbids geo-auto-merge → queue.
    d = classify_geo_pair(_gk(1, 101, cat="pozemek"), _gk(2, 102, cat="pozemek"), profile_for("pozemek"))
    assert d.action == "candidate" and d.reason == "geo_strong"


def test_classify_geo_weak_when_no_price_or_houseno_match() -> None:
    d = classify_geo_pair(_gk(1, 101, price=5_000_000), _gk(2, 102, price=9_000_000), profile_for("dum"))
    assert d.action == "candidate" and d.reason == "geo_weak"


def test_classify_geo_area_override_widens_the_candidate_gate() -> None:
    # A 15% area gap rejects under the profile's unified 10% but is a CANDIDATE when the
    # operator widens the geo tolerance to 20% (recall into the visual flow, not a merge).
    a, b = _gk(1, 101, area=100.0), _gk(2, 102, area=115.0)
    assert classify_geo_pair(a, b, profile_for("dum")).detail == "area_contradiction"
    d = classify_geo_pair(a, b, profile_for("dum"), max_area_pct=0.20)
    assert d.action != "reject"


def test_classify_geo_cross_type_dum_komercni_not_a_contradiction() -> None:
    # The geo path honours the same one cross-type as the street path: a dum + a komercni
    # at the same coordinate is the same building, NOT a category contradiction. But a
    # cross-type pair never geo-AUTO-merges (komercni isn't geo-auto-merge-validated) — it
    # QUEUES, symmetrically, regardless of which side arrived first.
    d = classify_geo_pair(
        _gk(1, 101, cat="dum", source="sreality"),
        _gk(2, 102, cat="komercni", source="idnes"),
        profile_for("dum"),
    )
    assert d.detail != "category_main_contradiction"
    assert d.action == "candidate" and d.reason == "geo_strong"


def test_classify_geo_cross_type_auto_merge_gate_is_order_independent() -> None:
    # The strong signal is identical both ways; the both-families gate must decide the SAME
    # (queue) whether dum or komercni is the first listing — no ordering-dependent auto-merge.
    forward = classify_geo_pair(
        _gk(1, 101, cat="dum"), _gk(2, 102, cat="komercni"), profile_for("dum"))
    reverse = classify_geo_pair(
        _gk(1, 101, cat="komercni"), _gk(2, 102, cat="dum"), profile_for("komercni"))
    assert forward.action == reverse.action == "candidate"
    assert forward.reason == reverse.reason == "geo_strong"


def test_classify_geo_same_type_dum_still_auto_merges() -> None:
    # Regression guard: the both-families gate must NOT change same-type behavior — a
    # dum+dum strong pair still auto-merges exactly as before.
    d = classify_geo_pair(_gk(1, 101), _gk(2, 102), profile_for("dum"))
    assert d.action == "auto_merge" and d.reason == "geo_exact"


def test_classify_geo_area_contradiction_rejects() -> None:
    d = classify_geo_pair(_gk(1, 101, area=100.0), _gk(2, 102, area=150.0), profile_for("dum"))
    assert d.action == "reject" and d.detail == "area_contradiction"


def test_classify_geo_house_number_contradiction_rejects() -> None:
    d = classify_geo_pair(_gk(1, 101, hn="10"), _gk(2, 102, hn="12"), profile_for("dum"))
    assert d.action == "reject" and d.detail == "house_number_contradiction"


def test_classify_geo_coord_too_far_rejects() -> None:
    # ~13 km apart — beyond the cell guard (defensive backstop).
    d = classify_geo_pair(
        _gk(1, 101, lat=50.10, lng=14.50), _gk(2, 102, lat=50.20, lng=14.60), profile_for("dum"),
    )
    assert d.action == "reject" and d.detail == "coord_too_far"


def test_classify_geo_unit_marker_contradiction_rejects() -> None:
    # Same-development guard: "pozemek č.3" vs "č.4" are distinct plots, never merge.
    d = classify_geo_pair(
        _gk(1, 101, cat="pozemek", desc="pozemek č.3 o velikosti 400 m²"),
        _gk(2, 102, cat="pozemek", desc="pozemek č.4 o velikosti 400 m²"),
        profile_for("pozemek"),
    )
    assert d.action == "reject" and d.detail == "unit_marker_contradiction"


def test_classify_geo_category_contradiction_rejects() -> None:
    # A genuine cross-category pair still rejects (byt vs dum). dum<->komercni is the ONE
    # sanctioned cross-type and is covered separately below.
    d = classify_geo_pair(_gk(1, 101, cat="byt"), _gk(2, 102, cat="dum"), profile_for("dum"))
    assert d.action == "reject" and d.detail == "category_main_contradiction"


def test_make_geo_classify_maps_auto_merge_to_candidate() -> None:
    # The unified geo path NEVER merges on the deterministic geo signal — _make_geo_classify
    # maps classify_geo_pair's auto_merge to candidate, so the free-first visual flow is the
    # sole merge gate. Rejects pass through; the operator's area tolerance is applied.
    import scripts.dedup_engine as eng
    fn = eng._make_geo_classify(0.20)
    # A strong same-type dum pair classify_geo_pair would auto_merge → mapped to candidate.
    strong = fn(_gk(1, 101, source="sreality"), _gk(2, 102, source="idnes"))
    assert strong.action == "candidate" and strong.reason == "geo_exact"
    # A genuine contradiction still rejects.
    assert fn(_gk(1, 101, cat="byt"), _gk(2, 102, cat="dum")).action == "reject"
    # The 0.20 tolerance accepts a 15% area gap the default 10% profile rejects.
    assert fn(_gk(1, 101, area=100.0), _gk(2, 102, area=115.0)).action != "reject"


def test_run_engine_geo_routes_through_resolve_pair_with_geo_tier(monkeypatch: Any) -> None:
    # The unification: a geo run loads geo-eligible listings and drives them through the
    # SAME resolve_pair brain — reaching the visual stage (cross-source), queuing with the
    # 'geo' tier, and NEVER merging on the deterministic geo signal alone.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality"), _gk(2, 102, source="idnes"),
    ])
    monkeypatch.setattr(eng, "merge_properties",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("geo signal must not merge")))
    enq: list[dict[str, Any]] = []
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda conn, x, y, markers, **kw: enq.append(markers))
    conn = _FakeConn([])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert stats["pairs_considered"] == 1      # reached the visual stage
    assert stats["queued"] == 1
    assert enq and enq[0]["tier"] == "geo"     # queued under the geo tier, not street


def test_run_engine_geo_skips_pair_when_both_sides_street_eligible(monkeypatch: Any) -> None:
    # Cross-pass ownership: a geo pair whose BOTH sides are street-eligible belongs to
    # the street pass — the geo pass skips it BEFORE any probe/budget spend and records
    # NOTHING (no reject, no dismissal, no queue row).
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality", street_eligible=True),
        _gk(2, 102, source="idnes", street_eligible=True),
    ])
    enq: list[dict[str, Any]] = []
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda conn, x, y, markers, **kw: enq.append(markers))
    conn = _FakeConn([])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert stats["pairs_considered"] == 0
    assert stats["rejected"] == 0 and stats["queued"] == 0
    assert not enq
    assert _dismissed_pairs(conn) == set()


def test_run_engine_geo_classifies_mixed_street_eligibility_pair(monkeypatch: Any) -> None:
    # The point of the relaxed geo load: a street-eligible dum row and a street-less dum
    # row of the same house CAN now pair — only the both-eligible case is skipped.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality", street_eligible=True),
        _gk(2, 102, source="idnes", street_eligible=False),
    ])
    enq: list[dict[str, Any]] = []
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda conn, x, y, markers, **kw: enq.append(markers))
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert stats["pairs_considered"] == 1
    assert stats["queued"] == 1
    assert enq and enq[0]["tier"] == "geo"


def test_run_engine_geo_does_not_skip_same_source(monkeypatch: Any) -> None:
    # The geo path has no rule B, so unlike the street path it must NOT drop same-source
    # pairs (a portal re-posting its own house) — they reach the visual stage.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality"), _gk(2, 102, source="sreality"),
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert "skipped_same_source" not in stats  # counter retired (PR-F)
    assert stats["pairs_considered"] == 1


# --- byt geo rung B: street-less apartments, cell + disposition -------------

def _bgk(
    sid: int, pid: int, *, source: str = "sreality", ct: str = "prodej",
    disp: str = "2+kk", floor: int | None = 3, area: float | None = 60.0,
    price: int | None = 5_950_000, hn: str | None = None,
    lat: float = 50.10064, lng: float = 14.53742, desc: str | None = None,
    street_eligible: bool = False, cat: str = "byt", lid: int | None = None,
) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid, source=source,
        street_key="geo:5001:50.1006:14.5374:byt:prodej|d:2+kk",
        disposition=disp, house_number=hn, floor=floor, area_m2=area,
        description=desc, category_type=ct, category_main=cat, street_id=None,
        lat=lat, lng=lng, price_czk=price, street_eligible=street_eligible,
        listing_id=lid if lid is not None else sid + 100_000,
    )


def test_classify_byt_geo_survivor_is_candidate_with_byt_geo_reason() -> None:
    d = classify_byt_geo_pair(
        _bgk(1, 101, source="sreality"), _bgk(2, 102, source="idnes"),
        profile_for("byt"))
    assert d.action == "candidate" and d.reason == "byt_geo"


def test_classify_byt_geo_never_auto_merges_even_on_strong_signals() -> None:
    # Identical coord + area + price + house number — the strongest attribute signal
    # the geo path would auto-merge for a dum. For byt there is NO auto_merge path at
    # all: one building stacks many identical-attribute units on one point.
    d = classify_byt_geo_pair(
        _bgk(1, 101, hn="12", area=60.0, price=5_000_000),
        _bgk(2, 102, hn="12", area=60.0, price=5_000_000),
        profile_for("byt"))
    assert d.action == "candidate" and d.reason == "byt_geo"


def test_classify_byt_geo_floor_any_known_difference_rejects() -> None:
    # Floors disambiguate units inside one building: ANY known difference rejects —
    # deliberately STRICTER than the street path's ±1 convention tolerance (a false
    # reject on a candidate-only rung costs recall, never a merge).
    d = classify_byt_geo_pair(
        _bgk(1, 101, floor=3), _bgk(2, 102, floor=4), profile_for("byt"))
    assert d.action == "reject" and d.detail == "floor_contradiction"


def test_classify_byt_geo_unknown_floor_side_is_candidate() -> None:
    d = classify_byt_geo_pair(
        _bgk(1, 101, floor=3), _bgk(2, 102, floor=None), profile_for("byt"))
    assert d.action == "candidate"


def test_classify_byt_geo_area_over_byt_10pct_rejects() -> None:
    # The byt profile's unified 10% gate — NOT the operator's wider geo tolerance
    # (stacked units differ mainly in area; the chain-merge hazard the 10% exists for).
    d = classify_byt_geo_pair(
        _bgk(1, 101, area=60.0), _bgk(2, 102, area=70.0), profile_for("byt"))
    assert d.action == "reject" and d.detail == "area_contradiction"
    ok = classify_byt_geo_pair(
        _bgk(1, 101, area=60.0), _bgk(2, 102, area=64.0), profile_for("byt"))
    assert ok.action == "candidate"


def test_classify_byt_geo_house_number_contradiction_rejects() -> None:
    d = classify_byt_geo_pair(
        _bgk(1, 101, hn="10"), _bgk(2, 102, hn="12"), profile_for("byt"))
    assert d.action == "reject" and d.detail == "house_number_contradiction"


def test_classify_byt_geo_coord_too_far_rejects() -> None:
    d = classify_byt_geo_pair(
        _bgk(1, 101, lat=50.10, lng=14.50), _bgk(2, 102, lat=50.20, lng=14.60),
        profile_for("byt"))
    assert d.action == "reject" and d.detail == "coord_too_far"


def test_classify_byt_geo_unit_marker_contradiction_rejects() -> None:
    d = classify_byt_geo_pair(
        _bgk(1, 101, desc="byt 42 v novostavbě"),
        _bgk(2, 102, desc="byt 45 v novostavbě"),
        profile_for("byt"))
    assert d.action == "reject" and d.detail == "unit_marker_contradiction"


def test_classify_byt_geo_category_type_contradiction_rejects() -> None:
    d = classify_byt_geo_pair(
        _bgk(1, 101, ct="prodej"), _bgk(2, 102, ct="pronajem"), profile_for("byt"))
    assert d.action == "reject" and d.detail == "category_type_contradiction"


def test_classify_byt_geo_category_main_contradiction_rejects() -> None:
    # Can't happen from the loader (byt buckets to its own cell) — the standalone
    # assertion still holds.
    d = classify_byt_geo_pair(
        _bgk(1, 101), _bgk(2, 102, cat="dum"), profile_for("byt"))
    assert d.action == "reject" and d.detail == "category_main_contradiction"


def test_classify_byt_geo_disposition_loose_compat_asserted() -> None:
    # The shard guarantees compatibility, but the classifier re-asserts it standalone:
    # 2+kk vs 2+1 are loose-compatible (candidate); 2+kk vs 3+kk reject.
    ok = classify_byt_geo_pair(
        _bgk(1, 101, disp="2+kk"), _bgk(2, 102, disp="2+1"), profile_for("byt"))
    assert ok.action == "candidate"
    d = classify_byt_geo_pair(
        _bgk(1, 101, disp="2+kk"), _bgk(2, 102, disp="3+kk"), profile_for("byt"))
    assert d.action == "reject" and d.detail == "disposition_mismatch"


def test_classify_byt_geo_same_listing_and_already_merged_reject() -> None:
    assert classify_byt_geo_pair(
        _bgk(1, 101), _bgk(1, 101), profile_for("byt")).detail == "same_listing"
    assert classify_byt_geo_pair(
        _bgk(1, 101), _bgk(2, 101), profile_for("byt")).detail == "already_merged"


def test_make_byt_geo_classify_candidate_and_reject_passthrough() -> None:
    import scripts.dedup_engine as eng
    fn = eng._make_byt_geo_classify()
    d = fn(_bgk(1, 101), _bgk(2, 102, source="idnes"))
    assert d.action == "candidate" and d.reason == "byt_geo"
    assert fn(_bgk(1, 101, floor=2), _bgk(2, 102, floor=5)).action == "reject"


def test_run_engine_byt_geo_routes_through_resolve_pair_with_byt_geo_tier(
        monkeypatch: Any) -> None:
    # The byt rung drives the SAME resolve_pair brain: the pair reaches the visual
    # stage, queues under the 'byt_geo' tier, and NEVER merges on the deterministic
    # cell signal alone.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _bgk(1, 101, source="sreality"), _bgk(2, 102, source="idnes"),
    ])
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("byt cell signal must not merge")))
    enq: list[dict[str, Any]] = []
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda conn, x, y, markers, **kw: enq.append(markers))
    conn = _FakeConn([])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None,
                           max_vision_calls=10, byt_geo=True)
    assert stats["pairs_considered"] == 1
    assert stats["queued"] == 1
    assert enq and enq[0]["tier"] == "byt_geo"


def test_run_engine_byt_geo_skips_pair_when_both_sides_street_eligible(
        monkeypatch: Any) -> None:
    # The #761 cross-pass skip is generalized to ALL non-street tiers: a byt_geo pair
    # whose BOTH sides are street-eligible belongs to the street pass — skipped before
    # any probe/budget spend, recording nothing.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _bgk(1, 101, street_eligible=True),
        _bgk(2, 102, source="idnes", street_eligible=True),
    ])
    enq: list[dict[str, Any]] = []
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda conn, x, y, markers, **kw: enq.append(markers))
    conn = _FakeConn([])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None,
                           max_vision_calls=10, byt_geo=True)
    assert stats["pairs_considered"] == 0
    assert stats["rejected"] == 0 and stats["queued"] == 0
    assert not enq
    assert _dismissed_pairs(conn) == set()


def test_run_engine_byt_geo_mixed_street_eligibility_pair_still_decided(
        monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _bgk(1, 101, street_eligible=True),
        _bgk(2, 102, source="idnes", street_eligible=False),
    ])
    enq: list[dict[str, Any]] = []
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda conn, x, y, markers, **kw: enq.append(markers))
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, byt_geo=True)
    assert stats["pairs_considered"] == 1 and stats["queued"] == 1
    assert enq and enq[0]["tier"] == "byt_geo"


def test_run_engine_geo_and_byt_geo_flags_are_mutually_exclusive() -> None:
    import scripts.dedup_engine as eng
    with pytest.raises(ValueError):
        eng.run_engine(_FakeConn([]), geo=True, byt_geo=True)


def test_run_engine_byt_geo_oversized_cell_processes_bounded(monkeypatch: Any) -> None:
    # Centroid-pinned byt cells reach 60-112 same-disposition members: they must ride
    # the SAME bounded value-ordered path as street/geo oversized groups, never a
    # whole-group skip (and never an unbounded O(n^2)).
    import scripts.dedup_engine as eng
    n = eng.MAX_GEO_GROUP_SIZE + 2
    members = [_bgk(i, 100 + i) for i in range(1, n + 1)]
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: members)
    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=0, byt_geo=True)
    total = n * (n - 1) // 2
    assert stats["oversized_groups"] == 1
    assert stats["pairs_considered"] == min(total, eng.MAX_GROUP_PAIRS)
    assert stats["skipped_oversized"] == max(0, total - eng.MAX_GROUP_PAIRS)


# --- non-byt attribute fast-path (dedup_nonbyt_attr_merge_enabled) ----------

def test_attr_exact_nonbyt_fires_on_area_within_2pct_and_exact_price() -> None:
    import scripts.dedup_engine as eng
    a = _gk(1, 101, area=120.0, price=5_950_000)
    b = _gk(2, 102, area=121.5, price=5_950_000)  # 1.5/121.5 = 1.23% area diff
    assert eng._attr_exact_nonbyt(a, b) is True


def test_attr_exact_nonbyt_rejects_area_over_2pct() -> None:
    import scripts.dedup_engine as eng
    a = _gk(1, 101, area=120.0, price=5_950_000)
    b = _gk(2, 102, area=123.5, price=5_950_000)  # 3.5/123.5 = 2.83% > 2%
    assert eng._attr_exact_nonbyt(a, b) is False


def test_attr_exact_nonbyt_rejects_non_exact_price() -> None:
    import scripts.dedup_engine as eng
    a = _gk(1, 101, area=120.0, price=5_950_000)
    b = _gk(2, 102, area=120.0, price=5_960_000)  # identical area, price off by 10k
    assert eng._attr_exact_nonbyt(a, b) is False


def test_attr_exact_nonbyt_rejects_null_area_or_price() -> None:
    import scripts.dedup_engine as eng
    assert eng._attr_exact_nonbyt(_gk(1, 101, area=None), _gk(2, 102)) is False
    assert eng._attr_exact_nonbyt(_gk(1, 101, price=None), _gk(2, 102)) is False


def test_run_engine_geo_attr_arm_merges_on_area_price_exact(monkeypatch: Any) -> None:
    # Flag ON: an area-within-2% + exact-price house pair auto-merges via the FREE attribute
    # arm — it never reaches the paid visual stage (pairs_considered stays 0).
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality", area=120.0, price=5_950_000),
        _gk(2, 102, source="idnes", area=121.0, price=5_950_000),  # 0.83% area, exact price
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           nonbyt_attr_merge=True)
    assert stats.get("auto_attr") == 1
    assert stats.get("pairs_considered", 0) == 0  # skipped the paid visual stage


def test_run_engine_geo_attr_arm_off_by_default_reaches_visual(monkeypatch: Any) -> None:
    # Flag OFF (default): the same pair falls through to the visual stage as before — no free
    # attr merge. Guards against the arm firing when the operator hasn't enabled it.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, area=120.0, price=5_950_000),
        _gk(2, 102, area=121.0, price=5_950_000),
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert stats.get("auto_attr", 0) == 0
    assert stats["pairs_considered"] == 1


def test_run_engine_byt_never_takes_attr_arm(monkeypatch: Any) -> None:
    # The arm is non-byt only (byt's area+price collide across a development's identical
    # units — the retired rule-B trap). A byt pair with the flag ON must NOT attr-merge.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, cat="byt", area=75.0, price=4_200_000),
        _gk(2, 102, cat="byt", area=75.0, price=4_200_000),
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           nonbyt_attr_merge=True)
    assert stats.get("auto_attr", 0) == 0


# --- §2.2 free arms: non-byt pHash single-pair + pair max-cosine ------------

def test_phash_fastpath_min_pairs_param() -> None:
    # Classic rule: 2 pairs (or a distinctive match). Arm (a) lowers min to 1 for non-byt.
    assert decide_phash_fastpath(1, False) is False                       # classic: 1 < 2
    assert decide_phash_fastpath(1, False, min_identical_pairs=1) is True  # arm (a)
    assert decide_phash_fastpath(0, False, min_identical_pairs=1) is False  # zero never fires
    assert decide_phash_fastpath(2, False) is True                        # classic unchanged
    assert decide_phash_fastpath(0, True, min_identical_pairs=1) is True  # distinctive unchanged


def test_run_engine_geo_phash_single_arm_merges_one_pair(monkeypatch: Any) -> None:
    # Flag ON: ONE pHash-identical pair merges a house pair via the fast-path, carrying the
    # DISTINCT reason 'phash_single' (funnel/unmerge attribution) and its own stats counter.
    import scripts.dedup_engine as eng
    merges: list[tuple[int, int, str]] = []
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality", price=5_950_000),
        _gk(2, 102, source="idnes", price=6_100_000),  # attr arm can't fire (price differs)
    ])
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(
            (survivor_id, retired_id, reason)) or {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 1)
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           nonbyt_phash_single=True)
    assert stats["auto_phash_single"] == 1
    assert stats.get("auto_phash", 0) == 0            # classic counter untouched
    assert stats.get("pairs_considered", 0) == 0      # never reached the paid stage
    assert merges == [(101, 102, "phash_single")]


def test_run_engine_geo_phash_single_off_keeps_two_pair_rule(monkeypatch: Any) -> None:
    # Flag OFF (default): one identical pair is NOT enough — the pair falls through to the
    # paid visual stage exactly as before.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, price=5_950_000),
        _gk(2, 102, price=6_100_000),
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 1)
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert stats.get("auto_phash_single", 0) == 0
    assert stats["pairs_considered"] == 1


def test_run_engine_byt_never_takes_phash_single(monkeypatch: Any) -> None:
    # Non-byt only: byt development renders collide across identical units, so byt keeps the
    # classic 2-pair rule even with the flag ON.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, cat="byt", price=5_950_000),
        _gk(2, 102, cat="byt", price=6_100_000),
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 1)
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           nonbyt_phash_single=True)
    assert stats.get("auto_phash_single", 0) == 0


def test_run_engine_geo_cosine_arm_merges_at_threshold(monkeypatch: Any) -> None:
    # Threshold set + both sides embedded + cosine above it → free merge with the DISTINCT
    # reason 'cosine_high'; the paid visual stage is never reached.
    import scripts.dedup_engine as eng
    import toolkit.clip_dedup as clip
    merges: list[tuple[int, int, str]] = []
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, source="sreality", price=5_950_000),
        _gk(2, 102, source="idnes", price=6_100_000),
    ])
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(
            (survivor_id, retired_id, reason)) or {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    monkeypatch.setattr(eng, "_clip_incomplete_any", lambda *a, **k: False)
    monkeypatch.setattr(clip, "pair_max_cosine", lambda *a, **k: 0.985)
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           clip_model="m", nonbyt_cosine_merge_min=0.98)
    assert stats["auto_cosine"] == 1
    assert stats.get("pairs_considered", 0) == 0
    assert merges == [(101, 102, "cosine_high")]


def test_run_engine_geo_cosine_below_threshold_or_missing_never_fires(monkeypatch: Any) -> None:
    # Below-threshold cosine and None (either side missing embeddings) both fall through to
    # the paid stage — None is "signal unavailable", never treated as a decision.
    import scripts.dedup_engine as eng
    import toolkit.clip_dedup as clip

    def _run(cos: Any) -> dict:
        monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
            _gk(1, 101, price=5_950_000),
            _gk(2, 102, price=6_100_000),
        ])
        monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
        monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)
        monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
        monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
        monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
        monkeypatch.setattr(eng, "_clip_incomplete_any", lambda *a, **k: False)
        monkeypatch.setattr(clip, "pair_max_cosine", lambda *a, **k: cos)
        return eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                              max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                              clip_model="m", nonbyt_cosine_merge_min=0.98)

    for cos in (0.97, None):
        stats = _run(cos)
        assert stats.get("auto_cosine", 0) == 0
        assert stats["pairs_considered"] == 1


def test_run_engine_geo_cosine_steps_aside_for_both_site_plans(monkeypatch: Any) -> None:
    # The development guard is unchanged: a both-site-plan pair skips the cosine arm and pays
    # the forensic same-unit path, exactly like pHash/attr.
    import scripts.dedup_engine as eng
    import toolkit.clip_dedup as clip
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, price=5_950_000),
        _gk(2, 102, price=6_100_000),
    ])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: True)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    monkeypatch.setattr(eng, "_clip_incomplete_any", lambda *a, **k: False)
    monkeypatch.setattr(clip, "pair_max_cosine", lambda *a, **k: 0.999)
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           clip_model="m", nonbyt_cosine_merge_min=0.98)
    assert stats.get("auto_cosine", 0) == 0
    assert stats["pairs_considered"] == 1


# --- download-completeness readiness (dedup_defer_incomplete_downloads) ------

def test_run_engine_defers_pair_while_image_downloading(monkeypatch: Any) -> None:
    # Flag ON + a side has an image pending download → the pair DEFERS: it never reaches the
    # attr arm or the paid visual stage, and re-decides for free once downloads/tags finish.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, area=120.0, price=5_950_000),
        _gk(2, 102, area=120.0, price=5_950_000),
    ])
    # Keyed on the SURROGATE listing_id (sid+100000): listing 1's pending download is id 100001.
    monkeypatch.setattr(eng, "_downloads_incomplete", lambda conn, sids: [s for s in sids if s == 100001])
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20,
                           defer_incomplete_downloads=True)
    assert stats.get("download_deferred") == 1
    assert stats.get("pairs_considered", 0) == 0  # never reached the visual stage
    assert stats.get("auto_attr", 0) == 0          # never reached the attr arm


def test_download_defer_off_by_default_ignores_pending_downloads(monkeypatch: Any) -> None:
    # Flag OFF (default): even with both sides mid-download the pair proceeds as today.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: [
        _gk(1, 101, area=120.0, price=5_950_000),
        _gk(2, 102, area=120.0, price=5_950_000),
    ])
    monkeypatch.setattr(eng, "_downloads_incomplete", lambda conn, sids: list(sids))
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True, geo_area_max_pct=0.20)
    assert stats.get("download_deferred", 0) == 0
    assert stats["pairs_considered"] == 1


def test_downloads_incomplete_any_memoizes_per_listing(monkeypatch: Any) -> None:
    # The cached wrapper queries each listing once per run (O(n)-per-group, like clip readiness).
    import scripts.dedup_engine as eng
    calls: list[list[int]] = []

    def _fake(conn: Any, sids: list[int]) -> list[int]:
        calls.append(list(sids))
        return [s for s in sids if s == 7]  # 7 is mid-download

    monkeypatch.setattr(eng, "_downloads_incomplete", _fake)
    cache = eng._ProbeCache()
    conn = _FakeConn([])
    assert eng._downloads_incomplete_any(conn, [7, 8], cache) is True
    assert eng._downloads_incomplete_any(conn, [8], cache) is False   # cached complete, no re-query
    assert eng._downloads_incomplete_any(conn, [7], cache) is True    # cached incomplete
    assert calls == [[7, 8]]  # only the first call hit the DB; the rest are memoized


def test_enqueue_candidate_tier_column_from_markers() -> None:
    # Regression: the tier COLUMN must reflect markers['tier'] (= ctx.tier), not the kwarg
    # default — else a geo candidate lands as 'street_disposition' and vanishes from the
    # geo queue even though its markers_matched jsonb says 'geo'.
    import scripts.dedup_engine as eng
    conn = _FakeConn([])
    eng._enqueue_candidate(conn, _gk(1, 101), _gk(2, 102), {"tier": "geo", "confidence": 0.6})
    assert conn.enqueued and conn.enqueued[0][2] == "geo"   # params = (lo, hi, TIER, conf, jsonb)
    # markers without a tier falls back to the kwarg default (the street rule-B path).
    conn2 = _FakeConn([])
    eng._enqueue_candidate(conn2, _gk(1, 101), _gk(2, 102), {"confidence": 0.9})
    assert conn2.enqueued[0][2] == "street_disposition"


def test_run_engine_geo_oversized_cell_processes_bounded(monkeypatch: Any) -> None:
    # An oversized geo cell is no longer skipped whole (the silent recall hole): it is
    # processed BOUNDED — best MAX_GROUP_PAIRS pairs in value order — and counted.
    import scripts.dedup_engine as eng
    n = eng.MAX_GEO_GROUP_SIZE + 2
    members = [_gk(i, 100 + i) for i in range(1, n + 1)]  # one oversized cell
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: members)
    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=0, geo=True)
    total = n * (n - 1) // 2
    assert stats["oversized_groups"] == 1
    assert stats["pairs_considered"] == min(total, eng.MAX_GROUP_PAIRS)
    assert stats["skipped_oversized"] == max(0, total - eng.MAX_GROUP_PAIRS)


# --- geo loader: the STORED listings.geo_cell_key is the blocking cell -------

def _geo_row(sid: int, pid: int, *, source: str = "sreality",
             hn: str | None = None, area: float | None = 120.0,
             description: str | None = None, ct: str | None = "prodej",
             cat: str | None = "dum", price: int | None = 5_950_000,
             lat: float | None = 50.10064, lng: float | None = 14.53742,
             cell: str | None = "geo:5001:50.1006:14.5374:dum|komercni:prodej",
             street_eligible: bool = False,
             disp: str | None = None, floor: int | None = None,
             lid: int | None = None,
             ) -> tuple[Any, ...]:
    # matches the _cell_eligible_sql column order (geo + byt_geo rungs):
    # sreality_id, property_id, source, house_number, area, description,
    # category_type, category_main, price_czk, lat, lng, geo_cell_key,
    # street_eligible, disposition, floor, id
    # listing_id defaults to sreality_id + 100000 — deliberately DIFFERENT from
    # sreality_id so a test that accidentally relied on sreality_id order would
    # fail loudly instead of silently passing on coincidental equality.
    return (sid, pid, source, hn, area, description, ct, cat, price, lat, lng, cell,
            street_eligible, disp, floor, lid if lid is not None else sid + 100_000)


def test_load_geo_eligible_uses_stored_cell_key_verbatim() -> None:
    # Migration 276: the loader takes listings.geo_cell_key from the SELECT — it must
    # NOT recompute the cell from lat/lng in Python. A stored key whose rendering
    # differs from the retired Python f-string (SQL trim_scale drops trailing zeros)
    # is taken as-is: SQL is the single definition of the blocking cell.
    import scripts.dedup_engine as eng

    stored = "geo:5001:50.1:14.5:dum|komercni:prodej"
    conn = _FakeConn([], geo_rows=[
        _geo_row(1, 101, cell=stored),
        _geo_row(2, 102, cell=stored),
    ])
    keys = eng._load_geo_eligible(conn)
    assert [k.street_key for k in keys] == [stored, stored]
    # lat/lng still ride along for the geo classifier's coordinate guard.
    assert keys[0].lat == pytest.approx(50.10064)
    assert keys[0].lng == pytest.approx(14.53742)
    assert keys[0].price_czk == 5_950_000 and keys[0].area_m2 == 120.0


def test_load_geo_eligible_carries_street_eligibility_and_drops_the_not_clause() -> None:
    # Cross-pass visibility: the geo load no longer excludes street-eligible rows —
    # it SELECTs the street predicate per row instead, and the loader stamps it on
    # ListingKey.street_eligible for resolve_pair's both-eligible skip.
    import scripts.dedup_engine as eng

    assert f"NOT ({eng._ELIGIBILITY})" not in eng._GEO_ELIGIBLE_SQL
    assert f"({eng._ELIGIBILITY}) AS street_eligible" in eng._GEO_ELIGIBLE_SQL
    conn = _FakeConn([], geo_rows=[
        _geo_row(1, 101, street_eligible=True),
        _geo_row(2, 102),
    ])
    keys = eng._load_geo_eligible(conn)
    assert [k.street_eligible for k in keys] == [True, False]


def test_load_geo_eligible_skips_null_stored_key() -> None:
    # A pre-backfill row (key not yet stamped) is skipped — it waits for the next
    # run after the backfill/trigger stamps it, and is never mis-grouped.
    import scripts.dedup_engine as eng

    conn = _FakeConn([], geo_rows=[
        _geo_row(1, 101, cell=None),
        _geo_row(2, 102),
    ])
    keys = eng._load_geo_eligible(conn)
    assert [k.sreality_id for k in keys] == [2]


def test_load_geo_eligible_restrict_cells_scope() -> None:
    """The --dirty geo sub-pass's scoped load: `restrict_cells` filters by the stored
    cell key (`= ANY`, an index seek on listings_geo_cell_key_idx) so the claimed
    properties' cells load WITH PEERS. An EMPTY set restricts to nothing (not all);
    mutually exclusive with restrict_property_ids."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}
    cell = "geo:5001:50.1006:14.5374:dum|komercni:prodej"

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [_geo_row(1, 101, cell=cell)]

    class _Conn:
        def cursor(self): return _C()

    keys = eng._load_geo_eligible(_Conn(), restrict_cells={cell})
    assert "AND l.geo_cell_key = ANY(%(cells)s)" in captured["sql"]
    assert captured["params"]["cells"] == [cell]
    assert keys and keys[0].sreality_id == 1 and keys[0].street_key == cell

    captured.clear()
    eng._load_geo_eligible(_Conn(), restrict_cells=set())   # empty != None
    assert captured["params"]["cells"] == []

    with pytest.raises(ValueError):
        eng._load_geo_eligible(_Conn(), restrict_property_ids={1}, restrict_cells={cell})


def test_load_byt_geo_eligible_shards_cells_by_disposition_class() -> None:
    """The byt rung's load: WHERE = BYT_GEO_ELIGIBLE_PREDICATE (disposition required),
    group key = stored cell + the street loader's `|d:{class}` shard suffix (2+kk and
    2+1 land in ONE shard — loss-free), and the ListingKey carries disposition + floor
    + street_eligible for classify_byt_geo_pair / the cross-pass skip."""
    import scripts.dedup_engine as eng
    from toolkit.publication import BYT_GEO_ELIGIBLE_PREDICATE

    assert BYT_GEO_ELIGIBLE_PREDICATE in eng._BYT_GEO_ELIGIBLE_SQL
    assert "l.disposition, l.floor" in eng._BYT_GEO_ELIGIBLE_SQL

    captured: dict[str, Any] = {}
    cell = "geo:5001:50.1006:14.5374:byt:prodej"

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split())
        def fetchall(self):
            return [
                _geo_row(1, 101, cat="byt", cell=cell, disp="2+kk", floor=3,
                         street_eligible=True),
                _geo_row(2, 102, cat="byt", cell=cell, disp="2+1", floor=None),
                _geo_row(3, 103, cat="byt", cell=cell, disp="3+kk", floor=2),
                _geo_row(4, 104, cat="byt", cell=None, disp="2+kk", floor=1),
            ]

    class _Conn:
        def cursor(self): return _C()

    keys = eng._load_geo_eligible(_Conn(), rung="byt_geo")
    assert "l.disposition IS NOT NULL" in captured["sql"]
    assert "category_main = 'byt'" in captured["sql"]
    assert [k.sreality_id for k in keys] == [1, 2, 3]        # NULL-cell row skipped
    # 2+kk and 2+1 share a shard (disposition_class); 3+kk is its own shard.
    assert keys[0].street_key == keys[1].street_key
    assert keys[0].street_key.startswith(cell + "|d:")
    assert keys[2].street_key != keys[0].street_key
    assert keys[0].disposition == "2+kk" and keys[0].floor == 3
    assert keys[1].floor is None
    assert keys[0].street_eligible is True and keys[1].street_eligible is False


def test_run_engine_threads_restrict_geo_cells_to_loader(monkeypatch: Any) -> None:
    # run_engine(geo=True, restrict_geo_cells=...) hands the cell scope AND the rung to
    # _load_geo_eligible — the seam the dirty cell sub-passes ride.
    import scripts.dedup_engine as eng

    seen: dict[str, Any] = {}

    def _fake_load(conn, restrict_property_ids=None, restrict_cells=None, rung="geo"):
        seen["cells"] = restrict_cells
        seen["pids"] = restrict_property_ids
        seen["rung"] = rung
        return []
    monkeypatch.setattr(eng, "_load_geo_eligible", _fake_load)
    eng.run_engine(_FakeConn([]), max_vision_calls=0, geo=True,
                   restrict_geo_cells={"cellA"})
    assert seen["cells"] == {"cellA"} and seen["pids"] is None
    assert seen["rung"] == "geo"

    seen.clear()
    eng.run_engine(_FakeConn([]), max_vision_calls=0, byt_geo=True,
                   restrict_geo_cells={"cellB"})
    assert seen["cells"] == {"cellB"} and seen["rung"] == "byt_geo"


def test_run_engine_geo_groups_by_stored_cell_key(monkeypatch: Any) -> None:
    # End-to-end over the fake conn (no loader monkeypatch): run_engine(geo=True)
    # loads through _GEO_ELIGIBLE_SQL and groups on the SELECTed stored cell key —
    # the same-cell cross-source pair reaches the visual stage; the different-cell
    # row pairs with nothing; the deterministic geo signal still never merges.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(
        eng, "merge_properties",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("geo signal must not merge")))
    conn = _FakeConn([], geo_rows=[
        _geo_row(1, 101, source="sreality"),
        _geo_row(2, 102, source="idnes"),
        _geo_row(3, 103, source="idnes", cat="pozemek",
                 cell="geo:5002:50.2:14.6:pozemek:prodej"),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None,
                           max_vision_calls=10, geo=True)
    assert stats["pairs_considered"] == 1
    assert stats["queued"] == 1


# --- rule D helpers ---------------------------------------------------------

def test_phash_fastpath_needs_two_identical_pairs() -> None:
    assert not decide_phash_fastpath(0)
    assert not decide_phash_fastpath(1)
    assert decide_phash_fastpath(2)
    assert decide_phash_fastpath(5)


def test_phash_fastpath_distinctive_single_match_overrides() -> None:
    # A single near-identical kitchen/bathroom match is enough (operator policy);
    # one generic match is not.
    assert decide_phash_fastpath(0, distinctive_match=True)
    assert decide_phash_fastpath(1, distinctive_match=True)
    assert not decide_phash_fastpath(1, distinctive_match=False)
    assert decide_phash_fastpath(2, distinctive_match=False)


def test_rooms_in_priority_orders_and_filters() -> None:
    common = {"bedroom", "kitchen", "bathroom", "floor_plan"}
    # floor_plan is not a comparison room -> excluded; kitchen before bathroom before bedroom
    assert rooms_in_priority(common) == ["kitchen", "bathroom", "bedroom"]


def test_rooms_in_priority_byt_excludes_exterior() -> None:
    # byt (default / explicit) compares INTERIOR rooms only — exterior_facade,
    # balcony_terrace and garden are dropped however the pair shares them.
    common = {"kitchen", "exterior_facade", "balcony_terrace", "garden", "bedroom"}
    assert rooms_in_priority(common, "byt") == ["kitchen", "bedroom"]
    assert rooms_in_priority(common) == ["kitchen", "bedroom"]  # default == byt


def test_rooms_in_priority_house_keeps_exterior() -> None:
    # Houses may use exterior — it is part of the property's identity there.
    common = {"kitchen", "exterior_facade", "bedroom"}
    assert "exterior_facade" in rooms_in_priority(common, "dum")


def test_room_priority_for_by_category() -> None:
    assert room_priority_for("byt")[0] == "kitchen"
    assert "exterior_facade" not in room_priority_for("byt")
    assert room_priority_for(None) == room_priority_for("byt")  # NULL -> apartment
    assert "exterior_facade" in room_priority_for("dum")


def test_phash_excluded_tags_for_by_category() -> None:
    # byt disqualifies known-exterior/shared images from the pHash count; other
    # categories exclude nothing (any image can carry their identity).
    excl = phash_excluded_tags_for("byt")
    assert "exterior_facade" in excl and "site_plan" in excl and "floor_plan" in excl
    assert "kitchen" not in excl and "bathroom" not in excl
    assert phash_excluded_tags_for(None) == excl  # NULL -> apartment
    assert phash_excluded_tags_for("dum") == ()
    assert phash_excluded_tags_for("pozemek") == ()


def test_phash_render_exclude_for_by_category() -> None:
    # byt excludes high render_score images from the pHash/cosine signal; other
    # categories don't (a render of a house IS that house's identity here).
    assert phash_render_exclude_for("byt") == RENDER_SCORE_EXCLUDE_MIN == 0.95
    assert phash_render_exclude_for(None) == RENDER_SCORE_EXCLUDE_MIN  # NULL -> apartment
    assert phash_render_exclude_for("dum") is None
    assert phash_render_exclude_for("pozemek") is None
    # the live app_settings value (dedup_render_exclude_min) overrides the default for byt
    assert phash_render_exclude_for("byt", 0.85) == 0.85
    assert phash_render_exclude_for("dum", 0.85) is None  # non-byt: still no exclusion


def test_render_exclusion_predicate_builds_clause() -> None:
    import scripts.dedup_engine as eng

    # neither filter -> empty string, no params bound
    p: dict = {}
    assert eng._render_exclusion_predicate(p, "ia", (), None) == ""
    assert p == {}
    # tags only
    p = {}
    sql = eng._render_exclusion_predicate(p, "ia", ("exterior_facade",), None)
    assert "ia.id" in sql and "logical_tag = ANY" in sql and "render_score" not in sql
    assert p["excl"] == ["exterior_facade"]
    # render only
    p = {}
    sql = eng._render_exclusion_predicate(p, "ib", (), 0.65)
    assert "ib.id" in sql and "render_score >= " in sql and "logical_tag" not in sql
    assert p["rmin"] == 0.65
    # both -> OR'd into one NOT EXISTS
    p = {}
    sql = eng._render_exclusion_predicate(p, "ia", ("garden",), 0.7)
    assert " OR " in sql and "logical_tag = ANY" in sql and "render_score >= " in sql
    assert p["excl"] == ["garden"] and p["rmin"] == 0.7


def test_verdict_gate_high_only() -> None:
    assert verdict_is_merge("High")
    assert not verdict_is_merge("Medium")
    assert not verdict_is_merge("Low")
    assert not verdict_is_merge(None)


# --- orchestration: run_engine against a fake conn --------------------------

class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append(s)
        if "count(*) FILTER" in s and "FROM listings" in s:
            self._rows = [(4, 100, 5)]  # eligible, flagged_location, flagged_disposition
        elif "l.geo_cell_key" in s and "FROM listings l" in s:
            # _GEO_ELIGIBLE_SQL (checked BEFORE the street branch — the geo SQL's
            # NOT-street-eligible clause also contains "l.street IS NOT NULL").
            self._rows = list(self._conn.geo_rows)
        elif "FROM listings l" in s and "l.street IS NOT NULL" in s:
            self._rows = list(self._conn.eligible_rows)
        elif "count(*)" in s and "JOIN properties pl" in s:
            self._rows = [(self._conn.stale_count,)]  # _reconcile_stale_candidates count
        elif "EXISTS (SELECT 1 FROM images ia" in s:
            self._rows = [(False,)]  # _phash_distinctive_match default (monkeypatch for a match)
        elif "GROUP BY 1, 2" in s and "FROM images ia JOIN images ib" in s:
            self._rows = []  # _phash_group_counts default: no near-identical pairs anywhere
        elif "FROM images ia JOIN images ib" in s:
            self._rows = [(0,)]  # _phash_identical_pairs default (tests monkeypatch when needed)
        elif "FROM images i WHERE i.listing_id" in s and "storage_path IS NOT NULL" in s:
            self._rows = []  # _floor_plan_image_ids default (no floor plan -> gate passes)
        elif "image_room_classifications" in s:
            self._rows = [(False,)]  # _both_have_site_plan default (CLIP-OR-LLM query)
        elif "UPDATE property_identity_candidates" in s:
            self._conn.resolved.append((s, params))  # reconcile / _resolve_candidates
            self._rows = []
        elif "INSERT INTO property_identity_candidates" in s and "SET status = 'proposed'" in s:
            self._conn.enqueued.append(params)  # reopen-variant enqueue (consult recall valve)
            self._rows = []
        elif "INSERT INTO property_identity_candidates" in s and "'dismissed'" in s:
            self._conn.dismiss_recorded.append(params)  # _record_auto_dismissed markers
            self._rows = []
        elif "INSERT INTO property_identity_candidates" in s:
            self._conn.enqueued.append(params)
            self._rows = []
        elif "UPDATE properties" in s and "published_at" in s:
            self._conn.publication_stamped.append(params)  # migration 273 publish gate
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, eligible_rows: list[tuple[Any, ...]], stale_count: int = 0,
                 geo_rows: list[tuple[Any, ...]] | None = None) -> None:
        self.eligible_rows = eligible_rows
        self.geo_rows = geo_rows or []
        self.stale_count = stale_count
        self.executed: list[str] = []
        self.enqueued: list[Any] = []
        self.dismiss_recorded: list[Any] = []  # _record_auto_dismissed marker INSERTs
        self.resolved: list[tuple[str, Any]] = []  # reconcile + _resolve_candidates UPDATEs
        self.publication_stamped: list[Any] = []  # _stamp_publication_checked UPDATEs (mig 273)

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Ctx:
        return _Ctx()

    def resolved_status(self, status: str) -> list[tuple[str, Any]]:
        """The _resolve_candidates UPDATEs that set candidates to `status`
        (params = (status, los, his))."""
        return [
            (s, p) for s, p in self.resolved
            if "FROM unnest(" in s and p and p[0] == status
        ]


def _row(sid: int, pid: int, *, street: str = "Nádražní",
         street_id: int | None = 42, disp: str = "2+kk",
         hn: str | None = "10", floor: int | None = 3, area: float | None = 60.0,
         source: str = "sreality", description: str | None = None,
         category_type: str | None = "prodej", category_main: str | None = "byt",
         obec_id: int | None = 5001, price: int | None = None,
         lid: int | None = None) -> tuple[Any, ...]:
    # matches _ELIGIBLE_SQL column order:
    # sreality_id, property_id, source, street, street_id, disposition,
    # house_number, floor, area_m2, description, category_type, category_main,
    # obec_id, price_czk, id
    # listing_id defaults to sreality_id + 100000 — deliberately DIFFERENT from
    # sreality_id so a test that accidentally relied on sreality_id order would
    # fail loudly instead of silently passing on coincidental equality.
    return (sid, pid, source, street, street_id, disp, hn, floor, area,
            description, category_type, category_main, obec_id, price,
            lid if lid is not None else sid + 100_000)


def test_run_engine_exact_address_no_longer_auto_merges(monkeypatch: Any) -> None:
    # Rule B RETIRED: an exact-address pair (same street/house/disp/floor/area) does NOT
    # auto-merge on address — it is a candidate that flows through pHash/visual like any other.
    # With no pHash match + no classifier here it reaches the visual stage and queues, never
    # `auto_address`. (No clip_model -> the tagging-readiness gate is inert in this test.)
    import scripts.dedup_engine as eng

    monkeypatch.setattr(
        eng, "merge_properties",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not auto-merge on address")))
    conn = _FakeConn([_row(1, 101), _row(2, 102)])  # exact-address pair
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)

    assert stats["auto_address"] == 0
    assert stats["pairs_considered"] == 1  # reached the rule-C / visual stage, not a rule-B merge


def test_run_engine_now_visually_compares_same_source(monkeypatch: Any) -> None:
    # Wave 3 removed the cross-source gate: a same-source rule-C candidate (shares
    # street+disposition, no exact-address rule B because house_number is absent) NOW reaches
    # the visual stage instead of being skipped — the recall change.
    import scripts.dedup_engine as eng

    conn = _FakeConn([
        _row(1, 101, hn=None, source="sreality"),
        _row(2, 102, hn=None, source="sreality"),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)

    assert "skipped_same_source" not in stats  # counter retired with the gate (PR-F)
    assert stats["pairs_considered"] == 1      # reached the visual stage


def test_run_engine_same_source_free_run_skips_not_queues(monkeypatch: Any) -> None:
    # The no-flood guarantee after removing the cross-source gate: on a FREE run
    # (enqueue_unresolved=False, no compare_fn) a same-source non-pHash pair reaches the visual
    # stage but is skipped_unresolved, NOT piled into the manual queue.
    import scripts.dedup_engine as eng

    conn = _FakeConn([
        _row(1, 101, hn=None, source="sreality"),
        _row(2, 102, hn=None, source="sreality"),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10,
                           enqueue_unresolved=False)

    assert stats["pairs_considered"] == 1
    assert stats["skipped_unresolved"] == 1
    assert stats["queued"] == 0


def test_run_engine_defers_on_incomplete_tagging(monkeypatch: Any) -> None:
    # Tagging-readiness gate (DEFAULT when a CLIP tagger is configured): a pair with a
    # NOT-fully-tagged listing DEFERS up front — before pHash, the floor-plan gate, or visual —
    # so the engine never decides on partial tag data. No re-queue (the pending image is already
    # in the clip_tag queue, clip_tagged_at IS NULL).
    import scripts.dedup_engine as eng

    # The readiness gate keys on the SURROGATE listing_id (not sreality_id); _row gives
    # listing 2 the id sid+100000, so "still tagging" is expressed as 100002.
    monkeypatch.setattr(eng, "_clip_incomplete", lambda conn, sids, model: [100002])
    monkeypatch.setattr(eng, "_trigger_clip_tagging",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-queue")))
    monkeypatch.setattr(eng, "merge_properties",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not merge")))
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])  # rule-C pair
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10,
                           clip_model="clip-x")
    assert stats["clip_deferred"] == 1
    # Deferred at the TOP of resolve_pair -> never reached the rule-C / visual stage.
    assert stats["queued"] == 0 and stats["pairs_considered"] == 0


def test_run_engine_fully_tagged_reaches_visual(monkeypatch: Any) -> None:
    # Both listings fully CLIP-tagged -> no defer, the pair reaches the visual stage normally.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "_clip_incomplete", lambda conn, sids, model: [])  # all complete
    monkeypatch.setattr(eng, "_trigger_clip_tagging",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not trigger")))
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10,
                           clip_model="clip-x")
    assert stats["clip_deferred"] == 0
    assert stats["pairs_considered"] == 1


def test_run_engine_no_clip_model_skips_readiness_gate(monkeypatch: Any) -> None:
    # No CLIP tagger configured (clip_model None): the readiness check never runs (the engine
    # falls back to its tag-agnostic flow — the readiness gate is CLIP-driven).
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "_clip_incomplete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("readiness must not run")))
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)
    assert stats["clip_deferred"] == 0


def test_trigger_clip_tagging_requeues_only_stuck_images() -> None:
    # The trigger resets clip_tagged_at ONLY on stuck images (marked done but tagless) — a
    # never-tagged image (clip_tagged_at IS NULL) is already pending, so it's left alone.
    import scripts.dedup_engine as eng

    conn = _FakeConn([])
    eng._trigger_clip_tagging(conn, [1, 2], "clip-x")
    sql = " ".join(conn.executed[-1].split())
    assert "UPDATE images SET clip_tagged_at = NULL" in sql
    assert "clip_tagged_at IS NOT NULL" in sql
    assert "NOT EXISTS" in sql and "image_clip_tags" in sql


def test_run_engine_does_not_skip_cross_source(monkeypatch: Any) -> None:
    # A cross-source candidate (sreality + bazos) DOES reach the visual stage.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    conn = _FakeConn([
        _row(1, 101, hn=None, source="sreality"),
        _row(2, 102, hn=None, source="bazos"),
    ])
    # No classify_fn -> _resolve_visual returns queue('no_images'); the point is it was REACHED.
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)

    assert "skipped_same_source" not in stats  # counter retired (PR-F)
    assert stats["pairs_considered"] == 1


def test_eligible_sql_includes_inactive_listings() -> None:
    # Price history must survive a delisting/relisting, so the engine considers
    # inactive listings too — the eligible scan and the counter must not gate on
    # is_active (the merge chokepoint gates on property status, not listing state).
    import scripts.dedup_engine as eng

    assert "is_active" not in eng._ELIGIBLE_SQL
    src = inspect.getsource(eng._eligibility_counts)
    assert "is_active" not in src


def _force_phash_merge(monkeypatch: Any, eng: Any) -> None:
    # Make any candidate pair merge via the pHash fast-path (>=2 matches, no site/floor plan),
    # the way exact-address pairs used to merge via the retired rule B.
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])


def test_run_engine_groups_id_keyed_sreality_with_name_keyed_bazos(monkeypatch: Any) -> None:
    # The cross-portal lever: the sreality row is id-keyed (street_id 42) but dual-keying also
    # lands it in the 'name:hlavni' group, where the bazos row (decorated street, no street_id)
    # meets it and the pHash fast-path merges them.
    import scripts.dedup_engine as eng

    merges: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append((survivor_id, retired_id, reason)) or {"data": {"merge_group_id": "g"}})
    _force_phash_merge(monkeypatch, eng)

    conn = _FakeConn([
        _row(1, 101, street="Hlavní", street_id=42),
        _row(2, 102, street="ul. Hlavní 12", street_id=None, source="bazos"),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)

    assert stats["auto_phash"] == 1
    assert merges == [(101, 102, "image_phash")]


def test_run_engine_dual_keys_act_once_per_listing_pair(monkeypatch: Any) -> None:
    # Two dual-keyed sreality rows share BOTH the id and the name group; the
    # pair must be classified + merged exactly once.
    import scripts.dedup_engine as eng

    merges: list[tuple[int, int, str]] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append((survivor_id, retired_id, reason)) or {"data": {"merge_group_id": "g"}})
    _force_phash_merge(monkeypatch, eng)

    conn = _FakeConn([
        _row(1, 101, street="Hlavní", street_id=42),
        _row(2, 102, street="Hlavní", street_id=42),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)

    assert stats["auto_phash"] == 1
    assert merges == [(101, 102, "image_phash")]
    assert stats["rejected"] == 0


def test_run_engine_same_name_different_street_id_rejects(monkeypatch: Any) -> None:
    # Same-named streets in two towns meet in the name group but their
    # canonical ids differ — hard reject, never a merge.
    import scripts.dedup_engine as eng

    merges: list[tuple[int, int, str]] = []

    def fake_merge(conn: Any, *, survivor_id: int, retired_id: int, reason: str, **kw: Any) -> dict:
        merges.append((survivor_id, retired_id, reason))
        return {"data": {"merge_group_id": "g"}}

    monkeypatch.setattr(eng, "merge_properties", fake_merge)

    conn = _FakeConn([
        _row(1, 101, street="Hlavní", street_id=42),
        _row(2, 102, street="Hlavní", street_id=99),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)

    assert merges == []
    assert stats["auto_address"] == 0
    assert stats["rejected"] == 1


def test_run_engine_phash_fastpath_merges_before_classify(monkeypatch: Any) -> None:
    # The free pHash tier runs BEFORE classify: >=2 raw matches -> auto-merge with
    # NO classify/compare. classify_fn is None to prove the LLM is never touched.
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert stats["vision_calls"] == 0
    assert merges == ["image_phash"]


def test_run_engine_phash_distinctive_single_match_merges(monkeypatch: Any) -> None:
    # #5: only ONE near-identical pair (below the >=2 generic bar), but it is a
    # kitchen/bathroom match -> distinctive override auto-merges.
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 1)  # below generic >=2
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: True)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert merges == ["image_phash"]


def test_run_engine_phash_single_generic_match_does_not_merge(monkeypatch: Any) -> None:
    # One generic (non-distinctive) near-identical pair is NOT enough -> no pHash merge.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 1)
    monkeypatch.setattr(eng, "_phash_distinctive_match", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 0


def test_run_engine_phash_fastpath_merges_same_source(monkeypatch: Any) -> None:
    # The pHash tier runs before the cross-source gate, so an identical-photo
    # SAME-source re-post merges for free (the recall the gate would otherwise drop).
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 4)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="sreality")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert "skipped_same_source" not in stats  # counter retired with the gate (PR-F)
    assert merges == ["image_phash"]


# --- #4 floor-plan validation gate (migration 234) -------------------------- #

def test_effective_vision_cap() -> None:
    import scripts.dedup_engine as eng

    # cache-only: never throttle warm reads
    assert eng._effective_vision_cap(
        free=False, cache_only=True, compare_budget=0, max_vision_calls=300) == 10_000_000
    # free (W2): the pool is the COMPARE budget — the forensic compares consume it; the
    # floor-plan gate runs on its OWN separate budget (run_engine.floor_plan_calls).
    assert eng._effective_vision_cap(
        free=True, cache_only=False, compare_budget=40, max_vision_calls=300) == 40
    # free + compare_budget 0 -> pHash-only free run: no forensic compares (the historical
    # --free behaviour). The floor-plan gate is unaffected — it has its own budget.
    assert eng._effective_vision_cap(
        free=True, cache_only=False, compare_budget=0, max_vision_calls=300) == 0
    # live (non-free) dispatch: the plain shared vision budget (the gate aliases into it)
    assert eng._effective_vision_cap(
        free=False, cache_only=False, compare_budget=40, max_vision_calls=300) == 300


def test_run_engine_only_groups_skips_untouched(monkeypatch: Any) -> None:
    """only_groups_with_property_ids (the real-time dirty drain): a street group is
    resolved ONLY if it contains a target property — full load (peers present), O(dirty)
    pair-work. The same pHash would-merge pair merges when its group is targeted, and is
    skipped (no merge) when it isn't."""
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)  # would merge
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])  # neither plan -> merge

    # group of two properties (101, 102); not targeted -> skipped, no merge
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats = eng.run_engine(conn, only_groups_with_property_ids={999}, max_vision_calls=10)
    assert stats["auto_phash"] == 0
    assert merges == []

    # same group, now targeted (contains 101) -> resolved, merges
    conn2 = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats2 = eng.run_engine(conn2, only_groups_with_property_ids={101}, max_vision_calls=10)
    assert stats2["auto_phash"] == 1
    assert merges == ["image_phash"]


def test_run_engine_truncated_flag() -> None:
    """run_engine sets stats['truncated'] when the deadline cuts the scan early (so the
    dirty drain keeps its claim and never drops unprocessed work); a finished run = 0."""
    import time

    import scripts.dedup_engine as eng

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats = eng.run_engine(conn, deadline=time.monotonic() - 1.0, max_vision_calls=0)
    assert stats["truncated"] == 1

    conn2 = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None)])
    stats2 = eng.run_engine(conn2, max_vision_calls=0)
    assert stats2["truncated"] == 0


def test_mark_dedup_dirty_empty_noop() -> None:
    """The dedup-ready enqueue is a no-op (no SQL) on an empty image-id list."""
    from scraper import db

    class _Conn:
        def cursor(self): raise AssertionError("must not touch the DB for an empty batch")

    assert db.mark_properties_dedup_dirty_for_images(_Conn(), []) == 0


def test_should_run_geo_only_on_explicit_flag() -> None:
    """Geo runs ONLY on an explicit flag — never auto-bolted onto the street full-scan /
    candidate runs (where it was deadline-starved / apartment-restricted and produced nothing)."""
    from scripts.dedup_engine import _should_run_geo

    # The dedicated --geo-only scheduled cron: gated by the dedup_geo_enabled master switch.
    assert _should_run_geo(geo=False, geo_only=True, geo_enabled=True, dirty=False) is True
    assert _should_run_geo(geo=False, geo_only=True, geo_enabled=False, dirty=False) is False
    # The street full-scan / candidate drain (no geo flag) NEVER auto-runs geo, even when enabled.
    assert _should_run_geo(geo=False, geo_only=False, geo_enabled=True, dirty=False) is False
    # --geo forces it onto any non-dirty run ad-hoc, ignoring the setting (debug).
    assert _should_run_geo(geo=True, geo_only=False, geo_enabled=False, dirty=False) is True
    # Never on the real-time dirty drain, whatever the flags.
    assert _should_run_geo(geo=True, geo_only=True, geo_enabled=True, dirty=True) is False


def test_should_run_byt_geo_gated_by_master_switch() -> None:
    """The byt geo rung's _should_run_geo mirror: ONLY the dedicated --byt-geo-only run,
    gated by dedup_byt_geo_enabled (registry default OFF), never on the dirty drain."""
    from scripts.dedup_engine import _should_run_byt_geo

    assert _should_run_byt_geo(byt_geo_only=True, enabled=True, dirty=False) is True
    assert _should_run_byt_geo(byt_geo_only=True, enabled=False, dirty=False) is False
    assert _should_run_byt_geo(byt_geo_only=False, enabled=True, dirty=False) is False
    assert _should_run_byt_geo(byt_geo_only=True, enabled=True, dirty=True) is False


def test_dedup_byt_geo_enabled_registry_default_off() -> None:
    """The rung ships OFF: the operator flips it after the migration-290 backfill."""
    from toolkit.dedup_settings import REGISTRY_BY_KEY, default_for

    assert default_for("dedup_byt_geo_enabled") is False
    assert REGISTRY_BY_KEY["dedup_byt_geo_enabled"].kind == "bool"


def test_dedup_byt_geo_cron_matches_its_args_branch() -> None:
    """The byt-geo cron string must be IDENTICAL in the schedule list AND the run-step
    match that selects --byt-geo-only — a drift would silently fall through to the
    catch-all free full-scan (the geo lane's historical bug)."""
    import pathlib

    wf = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dedup_engine.yml"
    text = wf.read_text()
    cron = "0 1,7,13,19 * * *"
    assert text.count(cron) >= 2, "byt-geo cron must appear in the schedule list AND the run-step match"
    branch = text.split(f'"{cron}" ]', 1)[1].split("elif", 1)[0]
    args = [ln for ln in branch.splitlines() if "ARGS=" in ln]
    assert any("--byt-geo-only" in ln for ln in args), "the byt-geo cron branch must set --byt-geo-only"
    assert not any("--free" in ln for ln in args), "the byt-geo cron branch must NOT be --free"


def test_dedup_geo_cron_matches_its_args_branch() -> None:
    """The geo cron string must be IDENTICAL in the schedule list AND the run-step match that
    selects --geo-only. If they drift, the cron fires but the elif chain falls through to the
    catch-all free full-scan — silently producing ZERO geo candidates again (the bug we fixed)."""
    import pathlib

    wf = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dedup_engine.yml"
    text = wf.read_text()
    cron = "0 3,9,15,21 * * *"
    assert text.count(cron) >= 2, "geo cron must appear in the schedule list AND the run-step match"
    branch = text.split(f'"{cron}" ]', 1)[1].split("elif", 1)[0]
    args = [ln for ln in branch.splitlines() if "ARGS=" in ln]
    assert any("--geo-only" in ln for ln in args), "the geo cron branch must set --geo-only"
    assert not any("--free" in ln for ln in args), "the geo cron branch must NOT be --free"


def test_dirty_cron_gets_its_own_concurrency_group() -> None:
    """The real-time DIRTY drain (:45) must run in a SEPARATE concurrency group from the slow
    batch runs, or the shared group starves/cancels it (killing the 'merge in minutes' SLO).
    The group expression keys off the SAME dirty cron string the run-step branch matches."""
    import pathlib

    wf = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "dedup_engine.yml"
    text = wf.read_text()
    dirty_cron = "45 * * * *"
    group_line = next(
        (ln for ln in text.splitlines() if ln.strip().startswith("group:") and "dedup-engine" in ln),
        "",
    )
    assert dirty_cron in group_line, "the concurrency group must branch on the dirty cron string"
    assert "-dirty" in group_line, "the dirty run must get its own '-dirty' concurrency group"


def test_claim_dedup_dirty_is_bounded_newest_first() -> None:
    """The --dirty claim is NEWEST-first + bounded: a real-time lane must serve the freshest
    dedup-ready listing first (the "merge in minutes" SLO holds under backlog), and stay bounded
    so a tagging-backlog spike can't make an hourly run claim the whole market. Unbounded only
    when no limit is passed (full-sweep / reconcile use)."""
    import scripts.dedup_engine as eng

    captured: list[tuple[str, list[Any]]] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *exc): return None
        def execute(self, sql, params=None): captured.append((sql, list(params or [])))
        def fetchall(self): return [(1,), (2,)]

    class _Conn:
        def cursor(self): return _Cur()

    eng._claim_dedup_dirty(_Conn(), "CUTOFF", limit=5000)
    sql, params = captured[-1]
    assert "ORDER BY marked_at DESC" in sql and "LIMIT %s" in sql
    assert params == ["CUTOFF", 5000]

    captured.clear()
    eng._claim_dedup_dirty(_Conn(), "CUTOFF")  # unbounded (full sweep / reconcile use)
    sql, params = captured[-1]
    assert "LIMIT" not in sql and params == ["CUTOFF"]


def test_prune_stale_dedup_dirty_evicts_by_ttl() -> None:
    """The TTL prune bounds the queue by construction (the guard the FIFO stall lacked): rows
    older than the TTL are deleted (the 6h full scan has already covered them), so an
    un-drainable backlog can never grow unbounded and the newest-first claim never hits a
    stale head. Default TTL >= the daily full-sweep cycle."""
    import scripts.dedup_engine as eng

    captured: list[str] = []

    class _Cur:
        rowcount = 7
        def __enter__(self): return self
        def __exit__(self, *exc): return None
        def execute(self, sql, params=None): captured.append(sql)

    class _Conn:
        def cursor(self): return _Cur()

    assert eng._DEDUP_DIRTY_TTL_HOURS >= 24
    assert eng._prune_stale_dedup_dirty(_Conn()) == 7
    assert "DELETE FROM dedup_dirty_properties" in captured[-1]
    assert "now() - interval" in captured[-1] and "hours" in captured[-1]


def test_dedup_dirty_enqueue_gates_eligible_and_recent() -> None:
    """The dedup-ready enqueue SQL keeps the real-time lane a CHANGE signal, not an
    enrichment-progress firehose: it enqueues a property ONLY when (a) it is fully tagged,
    (b) it has a listing the engine can REACH — street+disposition OR a geo-eligible
    single-dwelling row (property-grain EXISTS; the dirty pass resolves both families),
    and (c) the just-tagged listing is recent (a genuinely NEW arrival). Without these the
    market-wide CLIP backfill floods the queue (78.5% un-mergeable) and it stalls."""
    from scraper import db
    from toolkit.publication import eligible_predicate

    sql = db._DEDUP_DIRTY_FROM_IMAGE_IDS_SQL
    # eligibility: property-grain EXISTS over the property's listings, not the tagged one alone.
    assert "EXISTS" in sql and "le.property_id = l.property_id" in sql
    # the gate IS the shared street-OR-geo predicate rendered for the subquery alias —
    # single-sourced from toolkit.publication, never a hand copy.
    assert eligible_predicate("le") in sql
    # street arm: a street+disposition listing still qualifies.
    assert "le.street IS NOT NULL" in sql and "le.disposition IS NOT NULL" in sql
    # geo arm: a street-less dum/pozemek/komercni/ostatni listing with geom+obec+area
    # NOW enqueues too (the dirty pass grew a geo sub-pass)...
    assert "le.category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')" in sql
    assert "le.geom IS NOT NULL" in sql and "le.obec_id IS NOT NULL" in sql
    # ...and the byt-geo arm: a street-less byt with geom+obec+area+DISPOSITION rides
    # the real-time lane too (the dirty pass grew a byt-geo sub-pass, rung B).
    assert "le.category_main = 'byt'" in sql
    assert "le.category_main = 'byt'" in eligible_predicate("le")
    # recency: the tagged listing's first_seen_at within the window.
    assert "l.first_seen_at > now() - interval" in sql
    assert db._DEDUP_DIRTY_RECENCY_DAYS >= 1


def test_eligible_predicate_single_sources_the_parity_constants() -> None:
    """toolkit.publication.eligible_predicate is DERIVED from the same templates as the
    engine-verbatim parity constants — one text, three consumers (engine SQL, publish
    sweep, enqueue gate). Rendering another alias changes ONLY the alias."""
    from toolkit.publication import (
        BYT_GEO_ELIGIBLE_PREDICATE,
        GEO_ELIGIBLE_PREDICATE,
        STREET_ELIGIBLE_PREDICATE,
        eligible_predicate,
    )

    assert eligible_predicate("l") == (
        f"({STREET_ELIGIBLE_PREDICATE}) OR ({GEO_ELIGIBLE_PREDICATE}) OR "
        f"({BYT_GEO_ELIGIBLE_PREDICATE})")
    assert eligible_predicate("le") == eligible_predicate("l").replace("l.", "le.")


def test_run_engine_stats_carry_dirty_observability_keys() -> None:
    """run_engine's stats always carry dirty_cleared / dirty_truncated (NULL on non-dirty runs)
    so _write_run_row's %(dirty_cleared)s / %(dirty_truncated)s params never KeyError and the
    silent-livelock guard columns are always populated."""
    import scripts.dedup_engine as eng

    stats = eng.run_engine(_FakeConn([]), max_vision_calls=0)
    assert stats["dirty_cleared"] is None and stats["dirty_truncated"] is None
    assert "dirty_queue_depth" in stats and "dirty_claimed" in stats


def test_write_run_row_stamps_kind_truncated_started_at() -> None:
    """The run row records run_kind + the run-level truncated + the REAL started_at
    (migration 262). truncated defaults to 0 when the stats lack it; a truncated full
    scan writes 1 — the full-scan coverage-gap signal the 2026-07 audit found missing
    (every 6h scan silently deadline-cut at a fraction of the market)."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params

    class _Conn:
        def cursor(self): return _C()

    base: dict[str, Any] = {k: None for k in (
        "eligible", "flagged_location", "flagged_disposition", "pairs_considered",
        "rejected", "auto_address", "auto_phash", "auto_visual", "queued",
        "vision_calls", "auto_dismissed", "floor_plan_deferred", "clip_deferred",
        "clip_classified", "clip_cosine_calls", "routed_haiku", "routed_sonnet",
        "dirty_queue_depth", "dirty_claimed", "dirty_cleared", "dirty_truncated",
        "skipped_unresolved", "skipped_oversized", "oversized_groups", "vision_errors",
        "truncated_cause", "scan_groups_total", "scan_groups_scanned",
        "dirty_age_p95_seconds", "dirty_pruned",
    )}
    eng._write_run_row(_Conn(), {**base, "truncated": 1}, run_kind="full", started_at="T0")
    sql, params = captured["sql"], captured["params"]
    assert "started_at, ended_at, run_kind, truncated" in sql
    assert params["run_kind"] == "full" and params["truncated"] == 1
    assert params["started_at"] == "T0"

    eng._write_run_row(_Conn(), dict(base), run_kind="dirty", started_at="T0")
    assert captured["params"]["truncated"] == 0  # absent -> completed run


def _stub_resolve_pair(conn: Any, a: Any, b: Any, *, street_key: str, ctx: Any,
                       group_sids: Any = None) -> None:
    # Mirrors only resolve_pair's budget accounting — the per-group clear tracks group
    # completion, not pair outcomes, so a decide-nothing stub is enough.
    ctx.pairs_left -= 1
    ctx.stats["pairs_considered"] += 1


def test_run_engine_incremental_resolve_full_run(monkeypatch: Any) -> None:
    """A run that scans every group resolves ALL claimed properties — including a claimed
    property with NO eligible listing left (zero groups), which resolves immediately."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    conn = _FakeConn([
        _row(1, 101, street="Alfa", street_id=None, hn=None),
        _row(2, 102, street="Alfa", street_id=None, hn=None, source="bazos"),
        _row(3, 103, street="Beta", street_id=None, hn=None),
        _row(4, 104, street="Beta", street_id=None, hn=None, source="bazos"),
    ])
    claimed = {101, 102, 103, 104, 999}  # 999 = enqueued but no eligible listing anymore
    resolved: set[int] = set()
    stats = eng.run_engine(
        conn, only_groups_with_property_ids=claimed, resolved_property_ids=resolved,
        max_pairs=100, max_vision_calls=0)
    assert stats["truncated"] == 0
    assert resolved >= claimed


def test_run_engine_incremental_resolve_truncated_partial(monkeypatch: Any) -> None:
    """A pair-cap/deadline-truncated run resolves EXACTLY the fully-scanned groups'
    properties — the incremental clear's contract: monotonic progress, unfinished groups
    keep their claim. (Budget 2 over two 1-pair groups: group Alfa completes; the budget
    hits zero at Beta's boundary, so Beta counts as unfinished — conservative by design.)"""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    conn = _FakeConn([
        _row(1, 101, street="Alfa", street_id=None, hn=None),
        _row(2, 102, street="Alfa", street_id=None, hn=None, source="bazos"),
        _row(3, 103, street="Beta", street_id=None, hn=None),
        _row(4, 104, street="Beta", street_id=None, hn=None, source="bazos"),
    ])
    claimed = {101, 102, 103, 104}
    resolved: set[int] = set()
    stats = eng.run_engine(
        conn, only_groups_with_property_ids=claimed, resolved_property_ids=resolved,
        max_pairs=2, max_vision_calls=0)
    assert stats["truncated"] == 1
    assert resolved == {101, 102}


def test_run_engine_incremental_resolve_dual_key_guard(monkeypatch: Any) -> None:
    """A property dual-keys into its 'id:' AND 'name:' street groups; it must resolve only
    when BOTH are scanned. Truncating after the id-group leaves it unresolved (its name-group
    pairs were never re-decided), so its claim survives to the next run."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    conn = _FakeConn([
        _row(1, 201, street="Gama", street_id=77, hn=None),
        _row(2, 202, street="Gama", street_id=77, hn=None, source="bazos"),
    ])
    claimed = {201, 202}
    resolved: set[int] = set()
    stats = eng.run_engine(
        conn, only_groups_with_property_ids=claimed, resolved_property_ids=resolved,
        max_pairs=2, max_vision_calls=0)  # id-group's pair exhausts the budget at the boundary
    assert stats["truncated"] == 1
    assert resolved == set()  # id-group alone is NOT enough

    resolved2: set[int] = set()
    stats2 = eng.run_engine(
        _FakeConn([
            _row(1, 201, street="Gama", street_id=77, hn=None),
            _row(2, 202, street="Gama", street_id=77, hn=None, source="bazos"),
        ]),
        only_groups_with_property_ids=claimed, resolved_property_ids=resolved2,
        max_pairs=100, max_vision_calls=0)
    assert stats2["truncated"] == 0
    assert resolved2 >= claimed  # both groups scanned -> resolved


def test_run_engine_stamps_publication_checked(monkeypatch: Any) -> None:
    # Publication gate (migration 273): a non-dry run publishes every property whose
    # street group it scanned — the writer side of the properties_public gate. Fires on a
    # plain full scan (no resolved_property_ids), not just the dirty out-param path.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    conn = _FakeConn([
        _row(1, 101, street="Alfa", street_id=None, hn=None),
        _row(2, 102, street="Alfa", street_id=None, hn=None, source="bazos"),
        _row(3, 103, street="Beta", street_id=None, hn=None),
        _row(4, 104, street="Beta", street_id=None, hn=None, source="bazos"),
    ])
    eng.run_engine(conn, max_pairs=100, max_vision_calls=0)

    stamped = sorted(pid for e in conn.publication_stamped for pid in e["ids"])
    assert stamped == [101, 102, 103, 104]


def test_run_engine_dry_run_does_not_stamp_publication(monkeypatch: Any) -> None:
    # A shadow/dry run computes actions but writes nothing — no publish stamp (the gate
    # writer is guarded by `if not dry_run` in finalize()).
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    conn = _FakeConn([
        _row(1, 101, street="Alfa", street_id=None, hn=None),
        _row(2, 102, street="Alfa", street_id=None, hn=None, source="bazos"),
    ])
    eng.run_engine(conn, dry_run=True, max_pairs=100, max_vision_calls=0)

    assert conn.publication_stamped == []


def _three_group_rows() -> list[tuple[Any, ...]]:
    # Three single-key name groups, sorted alfa < beta < gama (street_id=None -> no id: group).
    return [
        _row(1, 101, street="Alfa", street_id=None, hn=None),
        _row(2, 102, street="Alfa", street_id=None, hn=None, source="bazos"),
        _row(3, 103, street="Beta", street_id=None, hn=None),
        _row(4, 104, street="Beta", street_id=None, hn=None, source="bazos"),
        _row(5, 105, street="Gama", street_id=None, hn=None),
        _row(6, 106, street="Gama", street_id=None, hn=None, source="bazos"),
    ]


def test_run_engine_cursor_resumes_after_key(monkeypatch: Any) -> None:
    """With cursor_out enabled the full scan iterates groups in SORTED order and resumes
    strictly AFTER scan_cursor — the frontier that lets successive deadline-bounded runs
    cover the whole market instead of head-restarting (the ~9%-coverage pathology)."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    cursor_out: dict[str, Any] = {}
    stats = eng.run_engine(
        _FakeConn(_three_group_rows()),
        scan_cursor="name:5001:alfa|d:2+1", cursor_out=cursor_out,
        max_pairs=100, max_vision_calls=0)
    assert stats["pairs_considered"] == 2  # beta + gama only; alfa skipped (behind cursor)
    assert cursor_out["last_key"].startswith("name:5001:gama")
    assert cursor_out["reached_end"] is True  # end of list -> cycle completes
    assert stats["scan_groups_total"] == 2 and stats["scan_groups_scanned"] == 2


def test_run_engine_cursor_truncation_reports_frontier(monkeypatch: Any) -> None:
    """A truncated cursor run reports the last fully-scanned group as the frontier and
    reached_end=False — the next run resumes there; the cycle does NOT complete."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    cursor_out: dict[str, Any] = {}
    stats = eng.run_engine(
        _FakeConn(_three_group_rows()),
        scan_cursor=None, cursor_out=cursor_out,
        max_pairs=2, max_vision_calls=0)  # budget dies at beta's boundary -> only alfa completes
    assert stats["truncated"] == 1
    assert stats["truncated_cause"] == "pair_cap"
    assert cursor_out["last_key"].startswith("name:5001:alfa")
    assert cursor_out["reached_end"] is False


# --- geo scan lane (PR-C): the same cursor machinery over geo cell keys ------

_GEO_CELL_A = "geo:5001:50.1:14.5:dum|komercni:prodej"
_GEO_CELL_B = "geo:5002:50.2:14.6:dum|komercni:prodej"
_GEO_CELL_C = "geo:5003:50.3:14.7:dum|komercni:prodej"


def _three_cell_geo_rows() -> list[tuple[Any, ...]]:
    # Three geo cells of two cross-source members each; the stored cell keys sort
    # lexically A < B < C (run_engine's cursor branch is key-agnostic).
    return [
        _geo_row(1, 101, cell=_GEO_CELL_A),
        _geo_row(2, 102, cell=_GEO_CELL_A, source="idnes"),
        _geo_row(3, 103, cell=_GEO_CELL_B),
        _geo_row(4, 104, cell=_GEO_CELL_B, source="idnes"),
        _geo_row(5, 105, cell=_GEO_CELL_C),
        _geo_row(6, 106, cell=_GEO_CELL_C, source="idnes"),
    ]


def test_run_engine_geo_cursor_resumes_after_cell_key(monkeypatch: Any) -> None:
    """The geo pass rides run_engine's existing cursor branch unchanged: with cursor_out
    enabled it iterates geo cells in SORTED stored-key order and resumes strictly AFTER
    scan_cursor — the lane='geo' frontier that stops the scheduled geo backstop from
    head-restarting at the top every run (the pre-mig-261 street pathology)."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    cursor_out: dict[str, Any] = {}
    stats = eng.run_engine(
        _FakeConn([], geo_rows=_three_cell_geo_rows()),
        geo=True, scan_cursor=_GEO_CELL_A, cursor_out=cursor_out,
        max_pairs=100, max_vision_calls=0)
    assert stats["pairs_considered"] == 2  # cells B + C only; A skipped (behind cursor)
    assert cursor_out["last_key"] == _GEO_CELL_C
    assert cursor_out["reached_end"] is True  # end of list -> cycle completes
    # The coverage gauges populate for geo runs too now that the caller passes cursor_out.
    assert stats["scan_groups_total"] == 2 and stats["scan_groups_scanned"] == 2


def test_run_engine_geo_cursor_truncation_reports_frontier(monkeypatch: Any) -> None:
    """A truncated geo cursor run reports the last fully-scanned CELL as the frontier and
    reached_end=False — the next scheduled run resumes there instead of the head."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    cursor_out: dict[str, Any] = {}
    stats = eng.run_engine(
        _FakeConn([], geo_rows=_three_cell_geo_rows()),
        geo=True, scan_cursor=None, cursor_out=cursor_out,
        max_pairs=2, max_vision_calls=0)  # budget dies at cell B's boundary
    assert stats["truncated"] == 1
    assert stats["truncated_cause"] == "pair_cap"
    assert cursor_out["last_key"] == _GEO_CELL_A
    assert cursor_out["reached_end"] is False


# --- Session 5: recency-first ordering composed with the cursor frontier -----

def test_run_engine_recency_head_jumps_queue_without_moving_cursor_backward(
    monkeypatch: Any,
) -> None:
    """A recency_head_property_ids group BEHIND the current scan_cursor still gets
    processed this run (the whole point — a fresh duplicate doesn't wait a full cycle
    for the lexicographic frontier to wrap back around to it), but the PERSISTED cursor
    position only ever advances over the cursor-ordered tail: a head-only visit must
    never regress scan_frontier, or a deadline-truncated run would walk the frontier
    backward every time the head is non-empty (defeating migration 261's guarantee)."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    cursor_out: dict[str, Any] = {}
    # Sorts strictly between alfa's and gama's keys regardless of the exact suffix
    # format (any street name < 'beta~' < any street name starting 'g...'), so alfa
    # (property 101) is behind the cursor and beta (property 103) is skipped like any
    # ordinary behind-cursor group; gama (property 105) is the only cursor-ordered tail.
    stats = eng.run_engine(
        _FakeConn(_three_group_rows()),
        scan_cursor="name:5001:beta~", cursor_out=cursor_out,
        recency_head_property_ids={101},
        max_pairs=100, max_vision_calls=0)
    assert stats["pairs_considered"] == 2  # alfa (head) + gama (tail); beta skipped
    assert cursor_out["last_key"].startswith("name:5001:gama")  # NOT alfa
    assert cursor_out["reached_end"] is True


def test_run_engine_recency_head_ignored_outside_cursor_runs(monkeypatch: Any) -> None:
    """recency_head_property_ids only means something to a cursor-bearing lane; a scoped
    run (candidates/dirty, cursor_out=None) ignores it rather than erroring."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    stats = eng.run_engine(
        _FakeConn(_three_group_rows()),
        recency_head_property_ids={101}, max_pairs=100, max_vision_calls=0)
    assert stats["pairs_considered"] == 3  # every group processed, insertion order


def _run_geo_only_main(monkeypatch: Any, *, reached_end: bool, last_key: str | None,
                       loaded_state: dict[str, Any] | None = None,
                       extra_argv: tuple[str, ...] = (),
                       flag: str = "--geo-only",
                       byt_geo_enabled: bool = False) -> dict[str, Any]:
    """Drive main() end-to-end for a --geo-only / --byt-geo-only run with the DB +
    settings + engine faked, capturing the scan-state lane traffic (load/save lane,
    run_engine kwargs)."""
    import sys
    import types

    import scripts.dedup_engine as eng
    import toolkit.dedup_settings as ds

    calls: dict[str, Any] = {"load": [], "save": [], "run_rows": [], "engine": []}

    class _Conn:
        def __enter__(self) -> "_Conn":
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def cursor(self) -> Any:
            raise AssertionError("unexpected direct DB access in this main() path")

    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["dedup_engine", flag, *extra_argv])
    monkeypatch.setitem(
        sys.modules, "psycopg",
        types.SimpleNamespace(connect=lambda *a, **k: _Conn()))

    settings = {
        "dedup_geo_enabled": True,
        "dedup_byt_geo_enabled": byt_geo_enabled,
        "dedup_geo_area_max_pct": 0.20,
        "dedup_floor_plan_budget": 0,
        "dedup_floor_plan_inconclusive_to_review": False,
        "dedup_nonbyt_attr_merge_enabled": False,
        "dedup_nonbyt_phash_single_enabled": False,
        "dedup_nonbyt_cosine_merge_min": 0,
        "dedup_facade_dismiss_enabled": False,
        "dedup_defer_incomplete_downloads": False,
        "dedup_engine_batch_defer_enabled": False,
    }
    monkeypatch.setattr(ds, "read_setting", lambda conn, key: settings[key])
    monkeypatch.setattr(eng, "_auto_merge_enabled", lambda conn: False)
    monkeypatch.setattr(eng, "_visual_autodismiss_enabled", lambda conn: True)
    monkeypatch.setattr(eng, "_clip_settings", lambda conn: {
        "prefer_clip": False, "clip_model": None, "cosine_enabled": False,
        "bands": None, "haiku_model": None, "render_min": 0.95,
    })

    state = loaded_state or {"cursor_key": None, "cycle_started_at": None}

    def _load(conn: Any, lane: str = "street") -> dict[str, Any]:
        calls["load"].append(lane)
        return dict(state)

    def _save(conn: Any, lane: str, *, cursor_key: str | None,
              cycle_started_at: Any, completed: bool) -> None:
        calls["save"].append({"lane": lane, "cursor_key": cursor_key,
                              "cycle_started_at": cycle_started_at,
                              "completed": completed})

    def _fake_run_engine(conn: Any, **kw: Any) -> dict[str, Any]:
        calls["engine"].append(kw)
        if kw.get("cursor_out") is not None:
            kw["cursor_out"]["last_key"] = last_key
            kw["cursor_out"]["reached_end"] = reached_end
        return {
            "eligible": 5, "auto_phash": 0, "auto_visual": 0, "auto_dismissed": 0,
            "floor_plan_deferred": 0, "queued": 1, "skipped_unresolved": 0,
            "rejected": 0, "pairs_considered": 3, "vision_calls": 0,
            "truncated": 0 if reached_end else 1,
        }

    monkeypatch.setattr(eng, "_load_scan_state", _load)
    monkeypatch.setattr(eng, "_save_scan_state", _save)
    monkeypatch.setattr(eng, "run_engine", _fake_run_engine)
    # Session 5: the geo/byt-geo full-scan branches compute a recency HEAD before
    # calling run_engine; this harness fakes run_engine itself, so fake the head query
    # too rather than hit the DB-access-forbidding _Conn.
    monkeypatch.setattr(eng, "_recency_head_candidate_ids", lambda conn, **kw: set())
    monkeypatch.setattr(
        eng, "_write_run_row",
        lambda conn, stats, **kw: calls["run_rows"].append({"stats": stats, **kw}))
    monkeypatch.setattr(eng, "_write_pair_audit", lambda *a, **k: None)

    assert eng.main() == 0
    return calls


def test_main_geo_only_truncated_run_advances_geo_lane_frontier(monkeypatch: Any) -> None:
    """main()'s geo branch mirrors the street full-scan branch: it loads lane='geo' scan
    state, threads the loaded cursor + a cursor_out into run_engine(geo=True), and saves
    the frontier (completed=False) when the run truncated mid-market."""
    calls = _run_geo_only_main(
        monkeypatch, reached_end=False, last_key=_GEO_CELL_B,
        loaded_state={"cursor_key": _GEO_CELL_A, "cycle_started_at": "T0"})

    assert calls["load"] == ["geo"]
    (engine_kw,) = calls["engine"]
    assert engine_kw["geo"] is True
    assert engine_kw["scan_cursor"] == _GEO_CELL_A       # resumes from the loaded frontier
    assert isinstance(engine_kw["cursor_out"], dict)     # gauges populate for geo runs
    (save,) = calls["save"]
    assert save == {"lane": "geo", "cursor_key": _GEO_CELL_B,
                    "cycle_started_at": "T0", "completed": False}
    assert calls["run_rows"] and calls["run_rows"][0]["run_kind"] == "geo"


def test_main_geo_only_completed_run_resets_geo_lane_cursor(monkeypatch: Any) -> None:
    """Reaching the end of the sorted cell list completes the lane='geo' CYCLE: the saved
    state resets the cursor (completed=True), so the next run starts a fresh cycle."""
    calls = _run_geo_only_main(monkeypatch, reached_end=True, last_key=_GEO_CELL_C)

    (save,) = calls["save"]
    assert save["lane"] == "geo"
    assert save["completed"] is True
    assert save["cursor_key"] is None
    # Fresh lane (no cycle_started_at loaded) -> the cycle stamp falls back to run start.
    assert save["cycle_started_at"] is not None


def test_main_geo_only_shadow_never_saves_scan_state(monkeypatch: Any) -> None:
    """--shadow writes nothing — the geo lane's frontier included (mirrors the street
    branch's not-args.shadow guard)."""
    calls = _run_geo_only_main(
        monkeypatch, reached_end=True, last_key=_GEO_CELL_C, extra_argv=("--shadow",))

    assert calls["load"] == ["geo"]  # still resumes from the frontier (read-only)
    assert calls["save"] == []
    assert calls["run_rows"] == []


def test_main_byt_geo_only_exits_when_setting_off(monkeypatch: Any) -> None:
    """--byt-geo-only is gated by the dedup_byt_geo_enabled master switch (registry
    default OFF): the run exits cleanly with NO engine work, no scan-state traffic,
    no run row — the scheduled cron is a no-op until the operator flips it."""
    calls = _run_geo_only_main(
        monkeypatch, reached_end=True, last_key=None,
        flag="--byt-geo-only", byt_geo_enabled=False)

    assert calls["engine"] == []
    assert calls["load"] == [] and calls["save"] == []
    assert calls["run_rows"] == []


def test_main_byt_geo_only_runs_own_lane_and_run_kind(monkeypatch: Any) -> None:
    """The enabled --byt-geo-only run mirrors the geo lane: lane='byt_geo' scan state,
    run_engine(byt_geo=True) with the loaded cursor + cursor_out, enqueue forced ON,
    its OWN run_kind='byt_geo' row, and a saved frontier on truncation."""
    calls = _run_geo_only_main(
        monkeypatch, reached_end=False, last_key="geo:1:50.1:14.5:byt:prodej|d:2+kk",
        loaded_state={"cursor_key": "geo:1:50.0:14.4:byt:prodej|d:1+kk",
                      "cycle_started_at": "T0"},
        flag="--byt-geo-only", byt_geo_enabled=True)

    assert calls["load"] == ["byt_geo"]
    (engine_kw,) = calls["engine"]
    assert engine_kw["byt_geo"] is True
    assert "geo" not in engine_kw or engine_kw.get("geo") is not True
    assert engine_kw["enqueue_unresolved"] is True
    assert engine_kw["scan_cursor"] == "geo:1:50.0:14.4:byt:prodej|d:1+kk"
    assert isinstance(engine_kw["cursor_out"], dict)
    (save,) = calls["save"]
    assert save == {"lane": "byt_geo",
                    "cursor_key": "geo:1:50.1:14.5:byt:prodej|d:2+kk",
                    "cycle_started_at": "T0", "completed": False}
    assert calls["run_rows"] and calls["run_rows"][0]["run_kind"] == "byt_geo"


def test_main_byt_geo_only_shadow_never_saves_scan_state(monkeypatch: Any) -> None:
    calls = _run_geo_only_main(
        monkeypatch, reached_end=True, last_key="k",
        flag="--byt-geo-only", byt_geo_enabled=True, extra_argv=("--shadow",))

    assert calls["load"] == ["byt_geo"]
    assert calls["save"] == []
    assert calls["run_rows"] == []


# --- oversized groups: disposition-class sharding + bounded processing -------

def test_disposition_class_loose_equivalence() -> None:
    # N+kk and N+1 share a class (classify_pair treats them compatible); unmapped
    # values are their own class. Sharding street groups by the class is loss-free.
    assert disposition_class("2+kk") == disposition_class("2+1")
    assert disposition_class("3+kk") == disposition_class("3+1")
    assert disposition_class("2+kk") != disposition_class("3+kk")
    assert disposition_class("atypicky") == "atypicky"
    assert disposition_class("6+kk") == "6+kk"
    assert disposition_class(None) == ""


def test_sharding_is_loss_free_for_compatible_pairs() -> None:
    # Any pair classify_pair would NOT reject on disposition shares a shard.
    for a, b in [("2+kk", "2+1"), ("2+1", "2+kk"), ("5+kk", "5+1"), ("atypicky", "atypicky")]:
        assert disposition_compatible(a, b)
        assert disposition_class(a) == disposition_class(b)


def test_load_eligible_shards_street_groups_by_disposition_class() -> None:
    # One street, two disposition classes -> TWO groups; loose pair (2+kk/2+1) stays together.
    import scripts.dedup_engine as eng

    conn = _FakeConn([
        _row(1, 101, street_id=None, disp="2+kk", hn=None),
        _row(2, 102, street_id=None, disp="2+1", hn=None, source="bazos"),
        _row(3, 103, street_id=None, disp="3+kk", hn=None),
    ])
    keys = eng._load_eligible(conn)
    groups = eng._group_by_street(keys)
    assert len(groups) == 2
    sizes = sorted(len(v) for v in groups.values())
    assert sizes == [1, 2]
    for key in groups:
        assert "|d:" in key


def _pk(sid: int, pid: int, source: str, price: int | None,
        area: float = 60.0) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid, source=source,
        street_key="name:5001:alfa|d:2+1", disposition="2+kk", house_number=None,
        floor=None, area_m2=area, category_type="prodej", category_main="byt",
        price_czk=price)


def test_prioritized_group_pairs_orders_and_caps() -> None:
    # Rejects dropped up front; dirty-touching pairs first, then cross-source,
    # then smaller price gap; cap respected.
    a = _pk(1, 101, "sreality", 5_000_000)
    b = _pk(2, 102, "bazos", 5_000_000)      # cross-source, exact price match with a
    c = _pk(3, 103, "sreality", 5_400_000)   # same-source vs a; cross vs b
    rejected = _pk(4, 104, "idnes", 5_000_000, area=120.0)  # >10% area gap -> rule-C reject

    pairs = prioritized_group_pairs([a, b, c, rejected], cap=100)
    flat = [(x.sreality_id, y.sreality_id) for x, y in pairs]
    assert all(4 not in p for p in flat)          # rejected pair never surfaces
    assert flat[0] == (1, 2)                      # cross-source + 0% price gap first
    assert set(flat) == {(1, 2), (2, 3), (1, 3)}

    dirty_first = prioritized_group_pairs([a, b, c], cap=100,
                                          priority_property_ids={103})
    flat2 = [(x.sreality_id, y.sreality_id) for x, y in dirty_first]
    assert 3 in flat2[0]                          # dirty-touching pair jumps the line

    capped = prioritized_group_pairs([a, b, c], cap=1)
    assert len(capped) == 1


def test_run_engine_oversized_street_group_processes_bounded(monkeypatch: Any) -> None:
    # 41 same-class members (> MAX_GROUP_SIZE=40): the group is processed bounded —
    # counted, best pairs first — instead of the historical silent whole-group skip.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    n = eng.MAX_GROUP_SIZE + 1
    conn = _FakeConn([
        _row(i, 100 + i, street="Laurinova", street_id=None, hn=None, floor=None,
             source=("sreality" if i % 2 else "bazos"))
        for i in range(1, n + 1)
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)
    total = n * (n - 1) // 2  # 820
    assert stats["oversized_groups"] == 1
    assert stats["pairs_considered"] == eng.MAX_GROUP_PAIRS
    assert stats["skipped_oversized"] == total - eng.MAX_GROUP_PAIRS


def test_run_engine_dirty_oversized_group_is_processed_not_poisoned(monkeypatch: Any) -> None:
    # The dirty-clear poisoning fix: an oversized group containing a claimed dirty
    # property is PROCESSED (bounded) before its members resolve — previously it was
    # skipped whole yet its dirty rows were cleared as if handled.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    n = eng.MAX_GROUP_SIZE + 1
    conn = _FakeConn([
        _row(i, 100 + i, street="Laurinova", street_id=None, hn=None, floor=None)
        for i in range(1, n + 1)
    ])
    claimed = {101}
    resolved: set[int] = set()
    stats = eng.run_engine(conn, only_groups_with_property_ids=claimed,
                           resolved_property_ids=resolved,
                           classify_fn=None, compare_fn=None, max_vision_calls=0)
    assert stats["pairs_considered"] > 0          # real pair work happened
    assert stats["oversized_groups"] == 1
    assert resolved >= claimed                    # cleared AFTER processing, not instead of


# --- candidates lifecycle: due-filter + engine-looked stamp (migration 272) ---

def test_proposed_candidate_property_ids_due_filter_sql() -> None:
    # With a backoff the drain loads only DUE candidates: never-stamped, backoff-elapsed,
    # or fresh CLIP evidence. Without one (None) the historical load-everything SQL runs.
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [(11, 22)]

    class _Conn:
        def cursor(self): return _Cur()

    out = eng._proposed_candidate_property_ids(_Conn(), redecide_hours=24)
    sql = captured["sql"]
    assert "last_engine_decision_at IS NULL" in sql
    assert "< now() - (%(backoff_h)s * interval '1 hour')" in sql  # float-safe form
    assert "i.clip_tagged_at > c.last_engine_decision_at" in sql
    assert captured["params"]["backoff_h"] == 24.0
    assert out == {11, 22}

    eng._proposed_candidate_property_ids(_Conn(), redecide_hours=None)
    assert "last_engine_decision_at" not in captured["sql"]  # historical full load


def test_recency_ranked_property_ids_orders_newest_first_and_dedupes() -> None:
    """The candidate drain's half of the Session 5 recency signal: rank an id set
    NEWEST-first by properties.first_seen_at (dedup'd + None-filtered on the way in)."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [(9,), (5,), (2,)]  # already newest-first per the ORDER BY

    class _Conn:
        def cursor(self): return _C()

    out = eng._recency_ranked_property_ids(_Conn(), [5, 9, 9, None, 2])
    assert out == [9, 5, 2]
    assert "ORDER BY first_seen_at DESC NULLS LAST" in captured["sql"]
    assert "LIMIT" not in captured["sql"]
    assert captured["params"]["ids"] == [2, 5, 9]  # deduped, sorted, None dropped

    assert eng._recency_ranked_property_ids(_Conn(), []) == []  # no round trip needed


def test_recency_ranked_property_ids_limit_bounds_the_ranking() -> None:
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [(9,)]

    class _Conn:
        def cursor(self): return _C()

    out = eng._recency_ranked_property_ids(_Conn(), [5, 9], limit=1)
    assert out == [9]
    assert "LIMIT %(limit)s" in captured["sql"]
    assert captured["params"]["limit"] == 1


def test_recency_head_candidate_ids_scopes_by_tier_and_window() -> None:
    """The sweep lanes' half of the Session 5 recency signal: still-proposed `tier`
    candidates whose newer side was first seen within the window — the SAME
    GREATEST(first_seen_at) basis as the dedup_recency_backlog acceptance-metric view
    (migration 307), bounded to `limit` pairs (a reserved slice, not a backlog dump)."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [(101, 202), (303, None)]

    class _Conn:
        def cursor(self): return _C()

    out = eng._recency_head_candidate_ids(_Conn(), tier="geo", window_days=3.0, limit=50)
    assert out == {101, 202, 303}
    sql, params = captured["sql"], captured["params"]
    assert "c.status = 'proposed' AND c.tier = %(tier)s" in sql
    assert "greatest(pl.first_seen_at, pr.first_seen_at)" in sql
    assert "ORDER BY greatest(pl.first_seen_at, pr.first_seen_at) DESC" in sql
    assert params == {"tier": "geo", "days": 3.0, "limit": 50}


def test_stamp_engine_looked_set_based_update() -> None:
    import scripts.dedup_engine as eng

    captured: list[tuple[str, Any]] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured.append((" ".join(sql.split()), params))

    class _Conn:
        def cursor(self): return _Cur()

    eng._stamp_engine_looked(_Conn(), {})
    assert captured == []                       # empty -> no round trip

    eng._stamp_engine_looked(_Conn(), {(5, 9): "visual_inconclusive", (2, 3): "clip_deferred"})
    sql, params = captured[-1]
    assert "SET last_engine_decision_at = now(), engine_decision = v.reason" in sql
    assert "c.status = 'proposed'" in sql
    # Session 5: write-once first_engine_decision_at (never overwritten on a re-decision
    # that leaves the pair proposed — COALESCE, not a plain assignment).
    assert "first_engine_decision_at = coalesce(c.first_engine_decision_at, now())" in sql
    pairs = dict(zip(zip(params["los"], params["his"]), params["reasons"]))
    assert pairs == {(5, 9): "visual_inconclusive", (2, 3): "clip_deferred"}


def test_run_engine_free_skip_stamps_engine_looked() -> None:
    # A free-mode pair that ends skipped_unresolved is STAMPED (the treadmill fix): the
    # candidate drain's due-filter will skip it until backoff or fresh CLIP evidence.
    import scripts.dedup_engine as eng

    conn = _FakeConn([
        _row(1, 101, hn=None, source="sreality"),
        _row(2, 102, hn=None, source="bazos"),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10,
                           enqueue_unresolved=False)
    assert stats["skipped_unresolved"] == 1
    stamps = [(s, p) for s, p in conn.resolved if "last_engine_decision_at = now()" in s]
    assert len(stamps) == 1
    _sql, params = stamps[0]
    assert params["los"] == [101] and params["his"] == [102]


# --- dirty lane: ordered claim + claim-order processing + run_dirty_pass -----

def test_claim_dedup_dirty_returns_newest_first_list() -> None:
    # The claim is an ORDERED list (newest-first) — returning a set discarded the
    # ORDER BY and let load-order (obec-ASC) head-of-line groups starve the queue head.
    import scripts.dedup_engine as eng

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            assert "ORDER BY marked_at DESC" in sql
        def fetchall(self):
            return [(7,), (5,), (9,)]

    class _Conn:
        def cursor(self): return _Cur()

    claimed = eng._claim_dedup_dirty(_Conn(), "T0", limit=10)
    assert claimed == [7, 5, 9]          # order preserved, not a set


def test_run_engine_processes_groups_in_claim_order(monkeypatch: Any) -> None:
    # priority_property_order ranks the scoped scan: the NEWEST claimed property's group
    # is processed first, so a budget-cut run spends its budget on the queue head.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "resolve_pair", _stub_resolve_pair)
    conn = _FakeConn([
        _row(1, 101, street="Alfa", street_id=None, hn=None),
        _row(2, 102, street="Alfa", street_id=None, hn=None, source="bazos"),
        _row(3, 201, street="Zeta", street_id=None, hn=None),
        _row(4, 202, street="Zeta", street_id=None, hn=None, source="bazos"),
    ])
    claimed_order = [201, 101]           # Zeta's property is the newest claim
    resolved: set[int] = set()
    stats = eng.run_engine(
        conn, only_groups_with_property_ids=set(claimed_order),
        resolved_property_ids=resolved, priority_property_order=claimed_order,
        max_pairs=2, max_vision_calls=0)  # budget covers Zeta fully, dies at Alfa's boundary
    assert stats["truncated"] == 1
    assert 201 in resolved and 202 in resolved   # newest group processed + cleared
    assert 101 not in resolved                   # older group keeps its claim


def test_run_dirty_pass_contract(monkeypatch: Any) -> None:
    # prune -> ordered claim -> scoped run (claim order threaded) -> incremental clear ->
    # run row with run_kind='dirty' + runner; empty queue returns None with NO run row.
    import scripts.dedup_engine as eng

    calls: dict[str, Any] = {}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): self._sql = sql
        def fetchone(self):
            return ("CUTOFF",) if "SELECT now()" in self._sql else (42,)

    class _Conn:
        def cursor(self): return _Cur()

    monkeypatch.setattr(eng, "_prune_stale_dedup_dirty", lambda conn: 3)
    monkeypatch.setattr(eng, "_claim_dedup_dirty",
                        lambda conn, cutoff, limit=None: [9, 5])
    monkeypatch.setattr(eng, "_dirty_queue_age_p95_seconds",
                        lambda conn, cutoff: 1234)
    monkeypatch.setattr(eng, "_claimed_street_groups",
                        lambda conn, pids: (set(), set()))
    monkeypatch.setattr(eng, "_claimed_family_eligibility",
                        lambda conn, pids: {9: (True, False, False), 5: (True, False, False)})
    monkeypatch.setattr(eng, "_claimed_geo_cells", lambda conn, pids, rung="geo": set())

    def _fake_run_engine(conn, **kw):
        calls["priority_property_order"] = kw["priority_property_order"]
        kw["resolved_property_ids"].update({9})
        return {"truncated": 0, "pairs_considered": 1}
    monkeypatch.setattr(eng, "run_engine", _fake_run_engine)

    def _fake_clear(conn, pids, cutoff):
        calls["cleared"] = sorted(pids)
        return 1
    monkeypatch.setattr(eng, "_clear_dedup_dirty", _fake_clear)
    monkeypatch.setattr(eng, "_write_run_row",
                        lambda conn, stats, *, run_kind, started_at, runner="actions":
                        calls.update(run_kind=run_kind, runner=runner, stats=dict(stats)))
    monkeypatch.setattr(eng, "_write_pair_audit", lambda conn, at, audit: None)

    stats = eng.run_dirty_pass(_Conn(), max_dirty=10, max_pairs=100, engine_kw={},
                               runner="worker", started_at="T0")
    assert stats is not None
    assert calls["priority_property_order"] == [9, 5]
    assert calls["cleared"] == [9]                      # only the resolved claim cleared
    assert calls["run_kind"] == "dirty" and calls["runner"] == "worker"
    assert calls["stats"]["dirty_claimed"] == 2
    assert calls["stats"]["dirty_age_p95_seconds"] == 1234
    assert calls["stats"]["dirty_pruned"] == 3
    assert calls["stats"]["dirty_cleared"] == 1

    # Empty queue: None, and no run row written.
    calls.clear()
    monkeypatch.setattr(eng, "_claim_dedup_dirty", lambda conn, cutoff, limit=None: [])
    assert eng.run_dirty_pass(_Conn(), max_dirty=10, max_pairs=100, engine_kw={}) is None
    assert "run_kind" not in calls


# --- dirty lane cell sub-passes (PR-B + byt rung): all families, one queue, one brain

def _dirty_pass_harness(
    monkeypatch: Any, *,
    claimed: list[int],
    family: dict[int, tuple[bool, bool, bool]],
    geo_cells: set[str] = frozenset(),
    byt_geo_cells: set[str] = frozenset(),
    street_resolves: set[int] = frozenset(),
    geo_resolves: set[int] = frozenset(),
    byt_geo_resolves: set[int] = frozenset(),
    street_stats: dict[str, Any] | None = None,
    geo_stats: dict[str, Any] | None = None,
    byt_geo_stats: dict[str, Any] | None = None,
) -> tuple[Any, Any, dict[str, Any]]:
    """Fake out run_dirty_pass's collaborators so the street/geo/byt-geo sub-pass
    orchestration (deadline handoff, kwargs derivation, per-family clear, single run
    row) is testable without a DB. `calls['engine']` records each run_engine
    invocation's kwargs."""
    import scripts.dedup_engine as eng

    calls: dict[str, Any] = {"engine": [], "cleared": None, "rows": []}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): self._sql = sql
        def fetchone(self):
            return ("CUTOFF",) if "SELECT now()" in self._sql else (42,)

    class _Conn:
        def cursor(self): return _Cur()

    monkeypatch.setattr(eng, "_prune_stale_dedup_dirty", lambda conn: 0)
    monkeypatch.setattr(eng, "_claim_dedup_dirty",
                        lambda conn, cutoff, limit=None: list(claimed))
    monkeypatch.setattr(eng, "_dirty_queue_age_p95_seconds", lambda conn, cutoff: None)
    monkeypatch.setattr(eng, "_claimed_street_groups", lambda conn, pids: (set(), set()))
    monkeypatch.setattr(eng, "_claimed_family_eligibility",
                        lambda conn, pids: dict(family))
    monkeypatch.setattr(
        eng, "_claimed_geo_cells",
        lambda conn, pids, rung="geo": set(byt_geo_cells if rung == "byt_geo" else geo_cells))
    import toolkit.dedup_settings as ds
    monkeypatch.setattr(ds, "read_setting", lambda conn, key: 0.2)

    def _fake_run_engine(conn, **kw):
        calls["engine"].append(kw)
        if kw.get("byt_geo"):
            kw["resolved_property_ids"].update(byt_geo_resolves)
            return dict(byt_geo_stats or {"truncated": 0, "pairs_considered": 1, "queued": 1})
        if kw.get("geo"):
            kw["resolved_property_ids"].update(geo_resolves)
            return dict(geo_stats or {"truncated": 0, "pairs_considered": 2, "queued": 1})
        kw["resolved_property_ids"].update(street_resolves)
        return dict(street_stats or {"truncated": 0, "pairs_considered": 1, "queued": 0})
    monkeypatch.setattr(eng, "run_engine", _fake_run_engine)

    def _fake_clear(conn, pids, cutoff):
        calls["cleared"] = sorted(pids)
        return len(pids)
    monkeypatch.setattr(eng, "_clear_dedup_dirty", _fake_clear)
    monkeypatch.setattr(eng, "_write_run_row",
                        lambda conn, stats, *, run_kind, started_at, runner="actions":
                        calls["rows"].append((run_kind, dict(stats))))
    monkeypatch.setattr(eng, "_write_pair_audit", lambda conn, at, audit: None)
    return eng, _Conn(), calls


def test_run_dirty_pass_street_only_claim_skips_geo(monkeypatch: Any) -> None:
    # (a) a street-only claim never touches the geo sub-pass (no geo cells), and clears
    # on the street resolve alone.
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[9], family={9: (True, False, False)},
        geo_cells=set(), street_resolves={9})
    stats = eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert stats is not None
    assert len(calls["engine"]) == 1 and not calls["engine"][0].get("geo")
    assert calls["cleared"] == [9]


def test_run_dirty_pass_geo_only_claim_runs_geo_subpass(monkeypatch: Any) -> None:
    # (b) a geo-only claim gets the SECOND run_engine(geo=True) over its stored cells —
    # enqueue_unresolved forced ON (tier-'geo' queue is geo's only surfacing mechanism),
    # scoped by restrict_geo_cells, same claim order — and clears on the GEO resolve
    # (the street sub-pass resolving it vacuously is not enough: se=False short-circuits,
    # ge=True requires dirty_resolved_geo).
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[7], family={7: (False, True, False)},
        geo_cells={"geo:5001:50.1:14.5:dum|komercni:prodej"}, geo_resolves={7})
    stats = eng.run_dirty_pass(
        conn, max_dirty=10, max_pairs=100,
        engine_kw={"enqueue_unresolved": False, "restrict_property_ids": None})
    assert stats is not None
    assert len(calls["engine"]) == 2
    street_kw, geo_kw = calls["engine"]
    assert not street_kw.get("geo")
    assert street_kw["enqueue_unresolved"] is False      # street posture untouched
    assert geo_kw["geo"] is True
    assert geo_kw["enqueue_unresolved"] is True          # geo forces the queue valve open
    assert geo_kw["restrict_geo_cells"] == {"geo:5001:50.1:14.5:dum|komercni:prodej"}
    assert geo_kw["restrict_property_ids"] is None       # cell scope, not property scope
    assert geo_kw["geo_area_max_pct"] == 0.2             # from dedup_geo_area_max_pct
    assert geo_kw["only_groups_with_property_ids"] == {7}
    assert geo_kw["priority_property_order"] == [7]
    assert calls["cleared"] == [7]


def test_run_dirty_pass_mixed_claim_clears_per_family(monkeypatch: Any) -> None:
    # (c) mixed claim: both sub-passes run; the street-only pid clears on the street
    # resolve while the geo-eligible pid — vacuously "resolved" by the street pass (it
    # has no street groups) — stays claimed until the GEO family resolves it.
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[9, 7], family={9: (True, False, False), 7: (False, True, False)},
        geo_cells={"cellA"}, street_resolves={9, 7}, geo_resolves=set())
    stats = eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert stats is not None
    assert len(calls["engine"]) == 2
    assert calls["cleared"] == [9]

    # and once the geo family resolves too, both clear; a neither-eligible pid clears
    # immediately (queue hygiene — the publish sweep owns its publication).
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[9, 7, 3],
        family={9: (True, False, False), 7: (False, True, False)},   # 3 absent = all-False
        geo_cells={"cellA"}, street_resolves={9, 7, 3}, geo_resolves={9, 7, 3})
    eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert calls["cleared"] == [3, 7, 9]


def test_run_dirty_pass_deadline_skips_geo_keeps_claims(monkeypatch: Any) -> None:
    # (d) the street sub-pass exhausting the SHARED wall-clock budget defers the geo
    # sub-pass whole: no second run_engine call, geo-only pids NOT cleared (they keep
    # their claim for the next pass), and the run row is marked truncated.
    import time as _time

    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[7, 9], family={7: (False, True, False), 9: (True, False, False)},
        geo_cells={"cellA"}, street_resolves={7, 9}, geo_resolves={7})
    stats = eng.run_dirty_pass(
        conn, max_dirty=10, max_pairs=100,
        engine_kw={"deadline": _time.monotonic() - 1.0})
    assert stats is not None
    assert len(calls["engine"]) == 1                     # geo sub-pass skipped
    assert calls["cleared"] == [9]                       # street-only pid still clears
    assert stats["truncated"] == 1
    assert stats["truncated_cause"] == "deadline"
    assert stats["dirty_truncated"] == 1


def test_run_dirty_pass_byt_only_claim_runs_byt_subpass(monkeypatch: Any) -> None:
    # A byt-geo-only claim gets a THIRD run_engine(byt_geo=True) over its stored byt
    # cells — enqueue forced ON (tier-'byt_geo' queue is the rung's surfacing
    # mechanism), cell-scoped, same claim order — and clears on the BYT resolve (the
    # street sub-pass resolving it vacuously is not enough).
    cell = "geo:5001:50.1006:14.5374:byt:prodej"
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[8], family={8: (False, False, True)},
        byt_geo_cells={cell}, byt_geo_resolves={8})
    stats = eng.run_dirty_pass(
        conn, max_dirty=10, max_pairs=100,
        engine_kw={"enqueue_unresolved": False, "restrict_property_ids": None})
    assert stats is not None
    assert len(calls["engine"]) == 2                     # street + byt (no geo cells)
    street_kw, byt_kw = calls["engine"]
    assert not street_kw.get("byt_geo") and not street_kw.get("geo")
    assert byt_kw["byt_geo"] is True and not byt_kw.get("geo")
    assert byt_kw["enqueue_unresolved"] is True
    assert byt_kw["restrict_geo_cells"] == {cell}
    assert byt_kw["restrict_property_ids"] is None       # cell scope, not property scope
    assert byt_kw["only_groups_with_property_ids"] == {8}
    assert byt_kw["priority_property_order"] == [8]
    assert calls["cleared"] == [8]


def test_run_dirty_pass_byt_family_gates_the_clear(monkeypatch: Any) -> None:
    # A byt-eligible pid vacuously "resolved" by the street pass stays claimed until
    # the BYT family resolves it — the third arm of the per-family clear.
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[9, 8],
        family={9: (True, False, False), 8: (False, False, True)},
        byt_geo_cells={"cellB"}, street_resolves={9, 8}, byt_geo_resolves=set())
    eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert calls["cleared"] == [9]

    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[9, 8],
        family={9: (True, False, False), 8: (False, False, True)},
        byt_geo_cells={"cellB"}, street_resolves={9, 8}, byt_geo_resolves={8})
    eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert calls["cleared"] == [8, 9]


def test_run_dirty_pass_deadline_defers_byt_subpass_keeps_claims(monkeypatch: Any) -> None:
    # An exhausted wall-clock budget defers the byt sub-pass whole, exactly like geo:
    # no byt run_engine call, the byt-only pid keeps its claim, run marked truncated.
    import time as _time

    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[8, 9],
        family={8: (False, False, True), 9: (True, False, False)},
        byt_geo_cells={"cellB"}, street_resolves={8, 9}, byt_geo_resolves={8})
    stats = eng.run_dirty_pass(
        conn, max_dirty=10, max_pairs=100,
        engine_kw={"deadline": _time.monotonic() - 1.0})
    assert stats is not None
    assert len(calls["engine"]) == 1                     # byt sub-pass skipped
    assert calls["cleared"] == [9]                       # street-only pid still clears
    assert stats["truncated"] == 1 and stats["truncated_cause"] == "deadline"


def test_run_dirty_pass_byt_subpass_is_ungated_by_master_switch(monkeypatch: Any) -> None:
    # Geo posture, mirrored: dedup_byt_geo_enabled gates ONLY the scheduled full rung
    # (--byt-geo-only); the dirty sub-pass runs regardless — it never even READS the
    # setting. This is what evaluates + publishes a new street-less byt in real time
    # while the scheduled rung is off.
    import toolkit.dedup_settings as ds

    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[8], family={8: (False, False, True)},
        byt_geo_cells={"cellB"}, byt_geo_resolves={8})
    read_keys: list[str] = []

    def _recording(conn, key):
        read_keys.append(key)
        return 0.2  # the harness stub value (any setting the pass legitimately reads)
    monkeypatch.setattr(ds, "read_setting", _recording)

    eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert any(kw.get("byt_geo") for kw in calls["engine"])
    assert "dedup_byt_geo_enabled" not in read_keys
    assert calls["cleared"] == [8]


def test_run_dirty_pass_aggregates_both_families_into_one_run_row(monkeypatch: Any) -> None:
    # (e) ONE run row per pass (run_kind='dirty'): the pair/merge/queue counters are
    # BOTH-family totals, truncated is OR'd, and the market gauges stay NULL (scoped
    # runs don't measure the market — the geo sub-pass's scoped eligible count must
    # not masquerade as one).
    eng, conn, calls = _dirty_pass_harness(
        monkeypatch, claimed=[9, 7], family={9: (True, False, False), 7: (False, True, False)},
        geo_cells={"cellA"}, street_resolves={9, 7}, geo_resolves={9, 7},
        street_stats={"eligible": None, "truncated": 0, "truncated_cause": None,
                      "pairs_considered": 3, "queued": 1, "auto_phash": 1,
                      "auto_visual": 0, "vision_calls": 2},
        geo_stats={"eligible": 12, "truncated": 1, "truncated_cause": "pair_cap",
                   "pairs_considered": 2, "queued": 2, "auto_phash": 0,
                   "auto_visual": 1, "vision_calls": 3})
    stats = eng.run_dirty_pass(conn, max_dirty=10, max_pairs=100, engine_kw={})
    assert stats is not None
    assert len(calls["rows"]) == 1
    kind, row = calls["rows"][0]
    assert kind == "dirty"
    assert row["pairs_considered"] == 5 and row["queued"] == 3
    assert row["auto_phash"] == 1 and row["auto_visual"] == 1
    assert row["vision_calls"] == 5
    assert row["eligible"] is None                       # gauge NOT summed
    assert row["truncated"] == 1 and row["truncated_cause"] == "pair_cap"
    assert row["dirty_claimed"] == 2 and row["dirty_cleared"] == 2


def test_merge_dirty_stats_shapes() -> None:
    # truncated ORs (never sums to 2); street cause wins when both truncated; a geo-only
    # counter fills a street None; gauges never fold across families.
    import scripts.dedup_engine as eng

    merged = eng._merge_dirty_stats(
        {"truncated": 1, "truncated_cause": "deadline", "pairs_considered": 1,
         "eligible": None, "clip_deferred": None},
        {"truncated": 1, "truncated_cause": "pair_cap", "pairs_considered": 2,
         "eligible": 10, "clip_deferred": 4})
    assert merged["truncated"] == 1
    assert merged["truncated_cause"] == "deadline"
    assert merged["pairs_considered"] == 3
    assert merged["eligible"] is None
    assert merged["clip_deferred"] == 4                  # None + int -> the int


def test_build_free_engine_kw_enqueue_unresolved_param(monkeypatch: Any) -> None:
    # Default False keeps the street --free posture (the /dedup queue never inflates
    # with un-vision'd street pairs); the geo sub-pass derives an enqueue-ON variant.
    import scripts.dedup_engine as eng
    import toolkit.dedup_settings as ds

    monkeypatch.setattr(eng, "_auto_merge_enabled", lambda conn: True)
    monkeypatch.setattr(eng, "_visual_autodismiss_enabled", lambda conn: True)
    monkeypatch.setattr(eng, "_clip_settings", lambda conn: {
        "prefer_clip": False, "clip_model": None, "cosine_enabled": False,
        "bands": None, "haiku_model": None, "render_min": 0.95})
    monkeypatch.setattr(ds, "read_setting", lambda conn, key: True)
    monkeypatch.setattr(eng, "_build_floor_plan_fn", lambda conn, **k: "fp")

    kw = eng.build_free_engine_kw(object(), compare_budget=0, floor_plan_budget=1)
    assert kw["enqueue_unresolved"] is False
    kw2 = eng.build_free_engine_kw(object(), compare_budget=0, floor_plan_budget=1,
                                   enqueue_unresolved=True)
    assert kw2["enqueue_unresolved"] is True


def test_run_realtime_dirty_pass_delegates_free_mode(monkeypatch: Any) -> None:
    # The worker's single entry point: assemble the --free engine_kw and delegate to
    # run_dirty_pass with runner='worker' and a deadline from max_seconds.
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}
    monkeypatch.setattr(eng, "build_free_engine_kw",
                        lambda conn, **kw: {"_kw": kw, "enqueue_unresolved": False})

    def _fake_dirty(conn, *, max_dirty, max_pairs, engine_kw, runner, stamp_stats=None):
        captured.update(max_dirty=max_dirty, max_pairs=max_pairs,
                        engine_kw=engine_kw, runner=runner)
        return {"dirty_claimed": 1}
    monkeypatch.setattr(eng, "run_dirty_pass", _fake_dirty)

    out = eng.run_realtime_dirty_pass(
        object(), max_dirty=200, compare_budget=4, floor_plan_budget=4, max_seconds=120)
    assert out == {"dirty_claimed": 1}
    assert captured["runner"] == "worker"
    assert captured["max_dirty"] == 200
    assert captured["engine_kw"]["_kw"]["compare_budget"] == 4
    assert captured["engine_kw"]["_kw"]["floor_plan_budget"] == 4
    assert captured["engine_kw"]["_kw"]["deadline"] is not None  # max_seconds>0 -> a deadline


def test_vision_error_breaker_helpers() -> None:
    # After VISION_ERROR_BREAKER errors the paid fns stop calling out (cache reads only) —
    # a dead key / exhausted credit degrades instead of burning the run budget on errors.
    import scripts.dedup_engine as eng

    err = [0]
    assert eng._breaker_open(err) is False
    assert eng._breaker_open(None) is False
    for _ in range(eng.VISION_ERROR_BREAKER):
        eng._count_vision_error(err)
    assert err[0] == eng.VISION_ERROR_BREAKER
    assert eng._breaker_open(err) is True
    eng._count_vision_error(None)  # no counter wired -> no-op


def test_prune_stale_dedup_dirty_is_cycle_gated() -> None:
    """TTL eviction must ALSO require coverage by a completed full-scan cycle
    (marked_at < dedup_scan_state.last_cycle_started_at) — with no completed cycle the NULL
    comparison evicts nothing, so eviction can never silently discard uncovered work."""
    import scripts.dedup_engine as eng

    captured: list[str] = []

    class _Cur:
        rowcount = 0
        def __enter__(self): return self
        def __exit__(self, *exc): return None
        def execute(self, sql, params=None): captured.append(sql)

    class _Conn:
        def cursor(self): return _Cur()

    eng._prune_stale_dedup_dirty(_Conn())
    sql = captured[-1]
    assert "last_cycle_started_at" in sql and "dedup_scan_state" in sql
    assert "lane = 'street'" in sql


def test_save_scan_state_shapes() -> None:
    """completed=True stamps the finished cycle + resets the cursor; completed=False only
    advances the frontier."""
    import scripts.dedup_engine as eng

    captured: list[tuple[str, Any]] = []

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *exc): return None
        def execute(self, sql, params=None): captured.append((sql, params))

    class _Conn:
        def cursor(self): return _Cur()

    eng._save_scan_state(_Conn(), "street", cursor_key="k", cycle_started_at="T", completed=False)
    sql, params = captured[-1]
    assert "cursor_key = EXCLUDED.cursor_key" in sql and params == ("street", "k", "T")

    eng._save_scan_state(_Conn(), "street", cursor_key=None, cycle_started_at="T", completed=True)
    sql, params = captured[-1]
    assert "last_cycle_started_at = EXCLUDED.last_cycle_started_at" in sql
    assert "cursor_key = NULL" in sql and params == ("street", "T")


def test_proposed_candidate_property_ids() -> None:
    """The candidate-drain work-list = every property in a still-proposed candidate
    (both sides, NULLs skipped, deduped)."""
    import scripts.dedup_engine as eng

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): self.sql = sql
        def fetchall(self): return [(101, 102), (103, None), (None, 104), (101, 105)]

    class _Conn:
        def cursor(self): return _C()

    assert eng._proposed_candidate_property_ids(_Conn()) == {101, 102, 103, 104, 105}


def test_load_eligible_restrict_scopes() -> None:
    """restrict=None -> no property filter (full scan); restrict=set() -> the filter IS
    applied (so an empty candidate queue loads NOTHING, never a full market scan)."""
    import scripts.dedup_engine as eng

    conn = _FakeConn([_row(1, 101)])
    eng._load_eligible(conn, restrict_property_ids=None)
    assert not any("l.property_id = ANY" in s for s in conn.executed)

    conn2 = _FakeConn([_row(1, 101)])
    eng._load_eligible(conn2, restrict_property_ids=set())  # empty != None
    assert any("l.property_id = ANY" in s for s in conn2.executed)


def test_load_eligible_street_group_scope() -> None:
    """The --dirty scoped load uses the targeted-seek CTE (unnest-JOINs per claimed key,
    NOT an OR that scans all eligible): a street_id arm + an obec-scoped name-key arm,
    UNION'd. An EMPTY (set, set) STILL takes the scoped SQL (empty unnests load nothing,
    never a full scan)."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):  # one eligible row (15 cols, incl. l.id) -> exercises ListingKey build
            return [(1, 101, "sreality", "Hlavní", None, "2+kk", "10", 3, 60.0,
                     "desc", "prodej", "byt", 42, 4_990_000, 100_001)]

    class _Conn:
        def cursor(self): return _C()

    keys = eng._load_eligible(
        _Conn(), restrict_street_groups=({5, 7}, {(42, "hlavni"), (-1, "maj")}))
    sql, params = captured["sql"], captured["params"]
    assert "WITH claimed AS" in sql
    assert "JOIN listings l ON l.street_id = s.id" in sql
    assert "coalesce(l.obec_id, -1) = g.o AND l.street_name_key = g.k" in sql
    assert "l.property_id = ANY" not in sql            # the property-id arm is NOT used here
    assert sorted(params["sids"]) == [5, 7]
    assert dict(zip(params["obecs"], params["keys"])) == {42: "hlavni", -1: "maj"}
    assert keys and keys[0].sreality_id == 1

    captured.clear()
    eng._load_eligible(_Conn(), restrict_street_groups=(set(), set()))
    assert "WITH claimed AS" in captured["sql"]
    assert captured["params"]["sids"] == [] and captured["params"]["keys"] == []


def test_claimed_street_groups() -> None:
    """The dirty work-list: positive street_ids + (coalesce(obec,-1), key) name-keys of the
    claimed properties' eligible listings; street_id<=0 and NULL keys are dropped, and an
    empty property set short-circuits without touching the DB."""
    import scripts.dedup_engine as eng

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): self.sql = sql
        def fetchall(self):  # SELECT DISTINCT street_id, coalesce(obec,-1), street_name_key
            return [(5, 42, "hlavni"), (0, 42, "hlavni"), (None, -1, "maj"), (7, 10, None)]

    class _Conn:
        def cursor(self): return _C()

    street_ids, name_keys = eng._claimed_street_groups(_Conn(), {101, 102})
    assert street_ids == {5, 7}                          # 0 dropped (not a real portal id)
    assert name_keys == {(42, "hlavni"), (-1, "maj")}    # NULL key row contributes only its id
    assert eng._claimed_street_groups(_Conn(), set()) == (set(), set())


def test_claimed_geo_cells_sql_shape() -> None:
    """The geo work-list: DISTINCT stored geo_cell_key of the claimed properties'
    geo-ELIGIBLE listings — mirrors _GEO_ELIGIBLE_SQL's WHERE (active single-dwelling
    with an area; street-eligible rows INCLUDED for cross-pass visibility) minus the
    properties join, cell required. Empty claim short-circuits without touching the
    DB."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [("geo:5001:50.1:14.5:dum|komercni:prodej",)]

    class _Conn:
        def cursor(self): return _C()

    cells = eng._claimed_geo_cells(_Conn(), {101, 102})
    assert cells == {"geo:5001:50.1:14.5:dum|komercni:prodej"}
    sql = captured["sql"]
    assert "SELECT DISTINCT l.geo_cell_key" in sql
    assert "l.property_id = ANY(%s)" in sql
    assert "l.geo_cell_key IS NOT NULL" in sql
    # the geo pass eligibility, single-sourced from toolkit.publication:
    assert "l.is_active = true" in sql
    assert "l.category_main IN ('dum', 'pozemek', 'komercni', 'ostatni')" in sql
    assert "coalesce(l.area_m2, l.estate_area, l.usable_area) IS NOT NULL" in sql
    # Street-eligible geo-family rows count for BOTH families now — the AND-NOT-street
    # exclusion must not re-grow here (resolve_pair skips both-eligible PAIRS instead).
    assert "NOT (l.street IS NOT NULL" not in sql
    assert sorted(captured["params"][0]) == [101, 102]

    # The byt rung swaps ONLY the eligibility predicate — same DISTINCT-cell shape.
    captured.clear()
    eng._claimed_geo_cells(_Conn(), {101}, rung="byt_geo")
    byt_sql = captured["sql"]
    assert "SELECT DISTINCT l.geo_cell_key" in byt_sql
    assert "l.geo_cell_key IS NOT NULL" in byt_sql
    assert "l.category_main = 'byt'" in byt_sql
    assert "l.disposition IS NOT NULL" in byt_sql
    assert "NOT (l.street IS NOT NULL" not in byt_sql

    class _NoDB:
        def cursor(self): raise AssertionError("must not query for an empty claim")

    assert eng._claimed_geo_cells(_NoDB(), set()) == set()
    assert eng._claimed_geo_cells(_NoDB(), set(), rung="byt_geo") == set()


def test_claimed_family_eligibility_sql_shape() -> None:
    """Per-pid (street, geo, byt_geo) eligibility in ONE grouped query — the per-family
    clear's key. Each cell arm requires the stored cell (== 'that sub-pass can load
    it'); missing pids read all-False; empty claim short-circuits."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        def fetchall(self):
            return [(9, True, False, False), (7, False, True, False),
                    (8, False, False, True), (3, None, None, None)]

    class _Conn:
        def cursor(self): return _C()

    fam = eng._claimed_family_eligibility(_Conn(), {9, 7, 8, 3})
    assert fam == {9: (True, False, False), 7: (False, True, False),
                   8: (False, False, True), 3: (False, False, False)}
    sql = captured["sql"]
    assert "SELECT l.property_id" in sql and "GROUP BY 1" in sql
    assert sql.count("bool_or(") == 3
    assert "l.property_id = ANY(%s)" in sql
    # BOTH cell arms require the stored cell (== loadable by their sub-pass).
    assert sql.count("l.geo_cell_key IS NOT NULL") == 2
    assert "l.street IS NOT NULL AND l.street <> '' AND l.disposition IS NOT NULL" in sql
    assert "l.category_main = 'byt'" in sql

    class _NoDB:
        def cursor(self): raise AssertionError("must not query for an empty claim")

    assert eng._claimed_family_eligibility(_NoDB(), set()) == {}


def test_resolve_pair_seam_standalone() -> None:
    """resolve_pair is callable standalone with a hand-built _RunContext — the exact seam
    the candidate-priority drain + the real-time per-listing path reuse (one decision tree,
    many drivers). A street_id contradiction rejects with no DB access."""
    import scripts.dedup_engine as eng
    from toolkit.dedup_engine import ListingKey

    a = ListingKey(1, 101, "sreality", "name:5001:nadrazni", "2+kk", "10", 3, 60.0,
                   street_id=1, listing_id=901)
    b = ListingKey(2, 102, "sreality", "name:5001:nadrazni", "2+kk", "10", 3, 60.0,
                   street_id=2, listing_id=902)
    ctx = eng._RunContext(stats={"rejected": 0})
    eng.resolve_pair(None, a, b, street_key="name:5001:nadrazni", ctx=ctx)
    assert ctx.stats["rejected"] == 1
    # the rejected pair is collected so the run finalize can dismiss any stale candidate
    assert (101, 102) in ctx.dismissed_pairs


def test_floor_plan_gate_branches(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng

    a = _key(1, pid=101, lid=901)
    b = _key(2, pid=102, lid=902)

    # neither side has a floor plan -> merge (existing path unchanged)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=None, vision_budget=[5]) == "merge"

    # exactly one side has a plan -> MERGE (contradiction-veto: no plan-to-plan compare is
    # possible, so the gate can't contradict the primary pHash/visual signal — it never queues).
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [9] if sid == 1 else [])
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=None, vision_budget=[5]) == "merge"

    # both have plans + a verdict available -> confirm/dismiss. floor_plan_fn now receives the
    # ListingKey pair (not raw ids) — the engine passes a.listing_id/b.listing_id downstream.
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [9])
    diff = lambda a, b, ia, ib: {"verdict": "different_layout"}  # noqa: E731
    same = lambda a, b, ia, ib: {"verdict": "same_layout"}       # noqa: E731
    none = lambda a, b, ia, ib: None                             # noqa: E731 (unwarmed)
    inconc = lambda a, b, ia, ib: {"verdict": "inconclusive"}    # noqa: E731
    no2d = lambda a, b, ia, ib: {"verdict": "no_2d_plan"}        # noqa: E731 (only 3D renders)
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=diff, vision_budget=[5]) == "dismiss"
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=same, vision_budget=[5]) == "merge"
    # 'no_2d_plan' (>=1 side only 3D renders / illegible) -> MERGE, NEVER queue, regardless of the
    # inconclusive toggle: the plan check is moot, so the primary signal stands (the 3D-render fix).
    assert eng._floor_plan_gate(
        None, a, b, floor_plan_fn=no2d, vision_budget=[5], inconclusive_to_review=True) == "merge"
    # 'inconclusive' (BOTH sides HAVE usable 2D plans, still ambiguous) -> manual review by default
    # (toggle on); off -> treat as no-contradiction -> merge.
    assert eng._floor_plan_gate(
        None, a, b, floor_plan_fn=inconc, vision_budget=[5],
        inconclusive_to_review=True) == "queue"
    assert eng._floor_plan_gate(
        None, a, b, floor_plan_fn=inconc, vision_budget=[5],
        inconclusive_to_review=False) == "merge"
    # both have plans but can't validate now (no fn / no budget / unwarmed) -> DEFER,
    # NOT the manual queue (the pair is automatable once the batch warms the verdict)
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=None, vision_budget=[5]) == "defer"
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=same, vision_budget=[0]) == "defer"
    assert eng._floor_plan_gate(None, a, b, floor_plan_fn=none, vision_budget=[5]) == "defer"
    # a COLD verdict consumes one budget unit
    budget = [3]
    eng._floor_plan_gate(None, a, b, floor_plan_fn=same, vision_budget=budget)
    assert budget[0] == 2


def test_run_engine_phash_floor_plan_different_dismisses(monkeypatch: Any) -> None:
    # pHash would merge, but the two floor plans differ -> dismiss (no merge).
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid])  # both have a plan
    fp = lambda a, b, ia, ib: {"verdict": "different_layout"}  # noqa: E731

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=fp, max_vision_calls=10)

    assert stats["auto_phash"] == 0
    assert stats["auto_dismissed"] == 1
    assert merges == []


def test_run_engine_phash_floor_plan_same_merges(monkeypatch: Any) -> None:
    # pHash would merge, the floor plans agree -> the merge proceeds.
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid])
    fp = lambda a, b, ia, ib: {"verdict": "same_layout"}  # noqa: E731

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=fp, max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert merges == ["image_phash"]


def test_run_engine_floor_plan_budget_separate_from_compare(monkeypatch: Any) -> None:
    """W2: the floor-plan gate runs on its OWN budget (run_engine.floor_plan_calls), so a
    zero COMPARE pool (max_vision_calls=0) can't starve it — the gate still fires its cold
    check on a would-merge pHash pair, and the call is counted in vision_calls. With the
    budget ALIASED (floor_plan_calls=None) the same zero pool DEFERS instead — the historical
    shared-pool behaviour, unchanged."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid])
    calls: list[int] = []

    def fp(a, b, ia, ib):
        calls.append(1)
        return {"verdict": "different_layout"}

    # Separate budget: compare pool 0, floor-plan budget 5 -> the gate STILL runs (dismiss).
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=fp,
                           max_vision_calls=0, floor_plan_calls=5)
    assert stats["auto_dismissed"] == 1
    assert stats["auto_phash"] == 0
    assert stats["vision_calls"] == 1  # the one cold floor-plan check, counted
    assert calls == [1]

    # Aliased (floor_plan_calls=None): the same zero pool starves the gate -> DEFER, no verdict.
    calls.clear()
    conn2 = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats2 = eng.run_engine(conn2, classify_fn=None, compare_fn=None, floor_plan_fn=fp,
                            max_vision_calls=0)
    assert stats2["auto_dismissed"] == 0
    assert stats2["floor_plan_deferred"] == 1
    assert stats2["vision_calls"] == 0
    assert calls == []  # gate never called the fn (budget 0, aliased)


def test_run_engine_phash_floor_plan_one_sided_merges(monkeypatch: Any) -> None:
    # pHash would merge and only ONE side has a floor plan -> the gate can't do a plan-to-plan
    # compare, so it doesn't contradict: the pHash merge PROCEEDS (contradiction-veto, migration
    # 260; was a manual queue before, which vetoed obvious cross-portal re-posts).
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid] if sid == 1 else [])

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert stats["queued"] == 0
    assert merges == ["image_phash"]


def test_run_engine_phash_floor_plan_no_2d_plan_merges(monkeypatch: Any) -> None:
    # Both sides carry a plan-tagged image, but the compare says no_2d_plan (only 3D renders /
    # illegible) -> the check is moot, so the pHash merge PROCEEDS (the 3D-render fix, migration 260).
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid])  # both have a plan-tag
    fp = lambda a, b, ia, ib: {"verdict": "no_2d_plan"}  # noqa: E731

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=fp, max_vision_calls=10)

    assert stats["auto_phash"] == 1
    assert stats["queued"] == 0
    assert merges == ["image_phash"]


def test_run_engine_phash_floor_plan_unwarmed_defers(monkeypatch: Any) -> None:
    # pHash would merge and BOTH sides have a floor plan, but no warm verdict is
    # available (floor_plan_fn=None, the free run before the batch lane warms the
    # cache) -> DEFER: not merged, not queued. The pair re-tries next run once warm.
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid])  # both have a plan

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 0
    assert stats.get("floor_plan_deferred", 0) == 1
    assert stats["queued"] == 0
    assert merges == []
    # the pair is NOT resolved either way -> still 'proposed' for a later warm run
    assert (101, 102) not in _dismissed_pairs(conn)


def test_run_engine_visual_high_but_different_floor_plan_dismisses(monkeypatch: Any) -> None:
    # A High forensic verdict is overridden by a different floor plan -> dismiss.
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)  # no pHash -> visual
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid])

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list,
                model: str | None = None) -> dict:
        return {"verdict": "High", "rationale": "matching tiles"}

    fp = lambda a, b, ia, ib: {"verdict": "different_layout"}  # noqa: E731

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=compare, floor_plan_fn=fp, max_vision_calls=10)

    assert stats["auto_visual"] == 0
    assert stats["auto_dismissed"] == 1
    assert merges == []


def test_run_engine_visual_high_merges_low_queues(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)  # no pHash -> falls to visual

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    # First pair: kitchen -> High. (single candidate pair). The 6th `model` arg
    # is the cosine-tier band route (None when the tier is off, as here).
    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list,
                model: str | None = None) -> dict:
        return {"verdict": "High", "rationale": "matching tiles"}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=classify, compare_fn=compare, max_vision_calls=10)
    assert stats["auto_visual"] == 1
    assert merges == ["visual_match"]
    assert stats["vision_calls"] == 1

    # Low verdict on a distinctive room (kitchen) -> AUTO-DISMISS, not queue.
    merges.clear()
    conn2 = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats2 = eng.run_engine(
        conn2, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "Low", "rationale": "different windows"},
        max_vision_calls=10,
    )
    assert stats2["auto_visual"] == 0
    assert stats2["auto_dismissed"] == 1
    assert stats2["queued"] == 0
    assert merges == []
    # the candidate pair is resolved to 'dismissed'
    dismissed = _dismissed_pairs(conn2)
    assert (101, 102) in dismissed


def test_run_engine_site_plan_different_unit_queues(monkeypatch: Any) -> None:
    """Both listings carry a site plan: the pHash fast-path DEFERS to the visual
    stage, and the 'different_unit' guard QUEUES instead of auto-merging — even
    though >=2 pHash matches would otherwise have merged."""
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 5)
    # Both have a site plan -> pHash defers to the development guard.
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: True)

    def classify(sid: int) -> dict:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "site_plan"},
            {"image_id": sid * 10 + 2, "room_type": "kitchen"},
        ]}}

    def site_plan(a: int, b: int, ids_a: list, ids_b: list, family: str | None = None) -> dict:
        return {"verdict": "different_unit", "rationale": "plot 3 vs plot 4"}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=lambda *a, **k: {"verdict": "High"},
        site_plan_fn=site_plan, max_vision_calls=10,
    )
    assert stats["queued"] == 1
    assert stats["auto_visual"] == 0 and stats["auto_phash"] == 0
    assert merges == []  # the development guard blocked the otherwise-certain pHash merge


def test_run_engine_site_plan_same_unit_falls_through_to_merge(monkeypatch: Any) -> None:
    """A 'same_unit' site-plan verdict does NOT block — after the pHash deferral the
    forensic compare still runs and a High verdict merges."""
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 5)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: True)  # defer to visual stage

    def classify(sid: int) -> dict:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "site_plan"},
            {"image_id": sid * 10 + 2, "room_type": "kitchen"},
        ]}}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=lambda *a, **k: {"verdict": "High", "rationale": "same kitchen"},
        site_plan_fn=lambda *a, **k: {"verdict": "same_unit", "rationale": "both plot 3"},
        max_vision_calls=10,
    )
    assert stats["auto_visual"] == 1
    assert merges == ["visual_match"]


def test_run_engine_rejects_floor_contradiction(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})

    conn = _FakeConn([_row(1, 101, floor=2), _row(2, 102, floor=8)])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)
    assert stats["rejected"] == 1
    assert stats["auto_address"] == 0 and stats["queued"] == 0


def test_run_engine_rejects_sale_vs_rent(monkeypatch: Any) -> None:
    """End-to-end: a sale and a rental on one street+disposition never merge and
    never queue — the reported /dedup bug."""
    import scripts.dedup_engine as eng
    merges: list[Any] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, **kw: merges.append(kw) or {"data": {"merge_group_id": "g"}},
    )
    conn = _FakeConn([
        _row(1, 101, category_type="prodej"),
        _row(2, 102, category_type="pronajem"),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)
    assert stats["rejected"] == 1
    assert stats["auto_address"] == 0 and stats["queued"] == 0
    assert merges == []


def test_run_engine_auto_merge_off_queues_exact_address(monkeypatch: Any) -> None:
    """Auto-merge toggle off: an exact-address pair queues for review, no merge."""
    import scripts.dedup_engine as eng

    merges: list[Any] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, **kw: merges.append(kw) or {"data": {"merge_group_id": "g"}},
    )
    conn = _FakeConn([_row(1, 101), _row(2, 102)])  # would normally auto-merge
    stats = eng.run_engine(
        conn, classify_fn=None, compare_fn=None, max_vision_calls=0,
        auto_merge_enabled=False,
    )
    assert merges == []
    assert stats["auto_address"] == 0
    assert stats["queued"] == 1
    assert len(conn.enqueued) == 1


def test_run_engine_auto_merge_off_queues_candidate_without_vision(monkeypatch: Any) -> None:
    """Auto-merge toggle off: a rule-C candidate queues without spending vision."""
    import scripts.dedup_engine as eng

    merges: list[Any] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, **kw: merges.append(kw) or {"data": {"merge_group_id": "g"}},
    )
    vision: list[int] = []

    def classify(sid: int) -> dict:
        vision.append(sid)
        return {"data": {"images": []}}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=lambda *a, **k: None,
        max_vision_calls=10, auto_merge_enabled=False,
    )
    assert merges == []
    assert stats["queued"] == 1
    assert vision == []  # no forensic vision spent when off


# --- decide_visual_dismiss (pure) -------------------------------------------

def test_decide_visual_dismiss_distinctive_low() -> None:
    assert decide_visual_dismiss({"kitchen": "Low"})
    assert decide_visual_dismiss({"bathroom": "Low"})
    assert decide_visual_dismiss({"kitchen": "Low", "bathroom": "Low"})
    # a distinctive Low alongside a generic Low still dismisses
    assert decide_visual_dismiss({"kitchen": "Low", "bedroom": "Low"})


def test_decide_visual_dismiss_blocks_on_any_high() -> None:
    # the OR-gate already merges on a High; never dismiss if any room matched
    assert not decide_visual_dismiss({"kitchen": "Low", "bathroom": "High"})
    assert not decide_visual_dismiss({"kitchen": "High"})


def test_decide_visual_dismiss_needs_a_distinctive_room() -> None:
    # only generic rooms compared -> keep for human review, don't dismiss
    assert not decide_visual_dismiss({"bedroom": "Low"})
    assert not decide_visual_dismiss({"living_room": "Low", "hallway": "Low"})
    assert not decide_visual_dismiss({})


def test_decide_visual_dismiss_blocks_on_distinctive_hedge() -> None:
    # a Medium hedge on a distinctive room -> not confident; keep for review
    assert not decide_visual_dismiss({"kitchen": "Medium"})
    assert not decide_visual_dismiss({"kitchen": "Low", "bathroom": "Medium"})


def test_facade_dismiss_off_by_default_and_byt_never_qualifies() -> None:
    # Default OFF: a facade Low alone never dismisses (the pre-§5.2 behavior).
    assert not decide_visual_dismiss({"exterior_facade": "Low"})
    assert not decide_visual_dismiss({"exterior_facade": "Low"}, "dum")
    # Flag ON but byt: the development shared-shell trap — facade never qualifies.
    assert not decide_visual_dismiss({"exterior_facade": "Low"}, "byt", facade_dismiss=True)
    assert not decide_visual_dismiss({"exterior_facade": "Low"}, None, facade_dismiss=True)


def test_facade_dismiss_on_for_nonbyt_families() -> None:
    for cat in ("dum", "pozemek", "komercni", "ostatni"):
        assert decide_visual_dismiss({"exterior_facade": "Low"}, cat, facade_dismiss=True)
    # conservatism unchanged: High still blocks, a facade Medium still queues
    assert not decide_visual_dismiss(
        {"exterior_facade": "Low", "garden": "High"}, "dum", facade_dismiss=True)
    assert not decide_visual_dismiss({"exterior_facade": "Medium"}, "dum", facade_dismiss=True)
    # a facade Low alongside a kitchen Medium is still a hedge on a qualifying room
    assert not decide_visual_dismiss(
        {"exterior_facade": "Low", "kitchen": "Medium"}, "dum", facade_dismiss=True)
    # generic-only rooms still never dismiss even with the flag on
    assert not decide_visual_dismiss({"garden": "Low"}, "pozemek", facade_dismiss=True)


# --- run_engine: self-healing (reconcile / dismiss / dry-run) ---------------

def test_run_engine_reject_dismisses_stale_candidate(monkeypatch: Any) -> None:
    # A pair the current rules REJECT (floor gap >=2) dismisses any stale proposed
    # candidate for it — recall-neutral queue hygiene.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})

    conn = _FakeConn([_row(1, 101, floor=2), _row(2, 102, floor=8)])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)

    assert stats["rejected"] == 1
    assert (101, 102) in _dismissed_pairs(conn)


def test_run_engine_visual_high_not_dismissed(monkeypatch: Any) -> None:
    # kitchen High -> merge, never the dismiss path.
    import scripts.dedup_engine as eng
    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "High", "rationale": "same kitchen"},
        max_vision_calls=10,
    )
    assert stats["auto_visual"] == 1 and stats["auto_dismissed"] == 0
    assert (101, 102) not in _dismissed_pairs(conn)


def test_run_engine_low_on_generic_room_queues_not_dismiss(monkeypatch: Any) -> None:
    # Only a generic room (living_room) compared + Low -> queue for review, NOT dismiss.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "living_room"}]}}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "Low", "rationale": "different"},
        max_vision_calls=10,
    )
    assert stats["auto_dismissed"] == 0
    assert stats["queued"] == 1


def test_run_engine_autodismiss_off_queues_low(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "Low", "rationale": "different"},
        max_vision_calls=10, autodismiss=False,
    )
    assert stats["auto_dismissed"] == 0
    assert stats["queued"] == 1


def test_run_engine_reconcile_counts_stale(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    conn = _FakeConn([], stale_count=42)
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0)
    assert stats["reconciled"] == 42


def test_run_engine_dry_run_writes_nothing(monkeypatch: Any) -> None:
    # Shadow: counts the would-merge but performs no merge + no candidate writes.
    import scripts.dedup_engine as eng
    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda *a, **k: merges.append("x") or {"data": {"merge_group_id": "g"}},
    )
    _force_phash_merge(monkeypatch, eng)
    conn = _FakeConn([_row(1, 101), _row(2, 102)])  # pHash -> would merge
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=0, dry_run=True)
    assert stats["auto_phash"] == 1     # counted
    assert merges == []                 # but not merged
    assert conn.resolved == []          # no candidate status writes
    assert conn.enqueued == []


def test_run_engine_partial_room_scan_does_not_dismiss(monkeypatch: Any) -> None:
    # The OR-gate guard: if the room cap stops the scan before every common room is
    # tried, a confident-Low distinctive room must NOT auto-dismiss — an untried
    # room might still match. kitchen Low but bedroom (untried, cap=1) -> QUEUE.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "kitchen"},
            {"image_id": sid * 10 + 2, "room_type": "bedroom"},
        ]}}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "Low", "rationale": "different"},
        max_vision_calls=10, max_room_attempts=1,  # stops after kitchen, bedroom untried
    )
    assert stats["auto_dismissed"] == 0
    assert stats["queued"] == 1
    assert (101, 102) not in _dismissed_pairs(conn)


def test_run_engine_warm_cache_hits_do_not_consume_budget(monkeypatch: Any) -> None:
    # Cost lever: a warm (cache_hit) compare is free and must NOT consume the cold
    # budget, so a tiny budget still applies unlimited already-paid-for verdicts.
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "kitchen"},
            {"image_id": sid * 10 + 2, "room_type": "bathroom"},
        ]}}

    # Both distinctive rooms warm + Low -> dismiss, even with cold budget = 1.
    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify,
        compare_fn=lambda *a, **k: {"verdict": "Low", "rationale": None, "cache_hit": True},
        max_vision_calls=1, max_room_attempts=4,
    )
    assert stats["auto_dismissed"] == 1
    assert stats["vision_calls"] == 0          # nothing cold spent
    assert (101, 102) in _dismissed_pairs(conn)


def test_run_engine_missing_room_verdict_blocks_dismiss(monkeypatch: Any) -> None:
    # A room whose compare returns None (un-warmed in cache-only / failed call) means
    # NOT every common room was verdicted -> never dismiss (an unseen room may match).
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "kitchen"},
            {"image_id": sid * 10 + 2, "room_type": "bathroom"},
        ]}}

    # kitchen Low (warm), bathroom un-warmed (None) -> not all rooms verdicted -> queue.
    def compare(a, b, room, ids_a, ids_b, model=None):
        return {"verdict": "Low", "cache_hit": True} if room == "kitchen" else None

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=compare,
        max_vision_calls=10, max_room_attempts=4,
    )
    assert stats["auto_dismissed"] == 0
    assert stats["queued"] == 1


def test_run_engine_free_mode_skips_unresolved(monkeypatch: Any) -> None:
    # Free mode ($0): an un-vision'd cross-source candidate is SKIPPED, not queued
    # as a 'no photos compared' placeholder (so the review queue doesn't inflate).
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "merge_properties", lambda *a, **k: {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)  # no pHash

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=None, compare_fn=None, max_vision_calls=0,
        enqueue_unresolved=False,
    )
    assert stats["skipped_unresolved"] == 1
    assert stats["queued"] == 0
    assert conn.enqueued == []  # no placeholder written


def test_run_engine_free_mode_phash_still_merges(monkeypatch: Any) -> None:
    # Free mode still harvests the free pHash merges.
    import scripts.dedup_engine as eng
    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: merges.append(reason) or {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=None, compare_fn=None, max_vision_calls=0,
        enqueue_unresolved=False,
    )
    assert stats["auto_phash"] == 1
    assert merges == ["image_phash"]


# ---- Stage 4b: CLIP cosine -> forensic-model band routing (pure) ----------


def test_route_by_cosine_high_band_routes_to_haiku() -> None:
    bands = CosineBands(haiku_min=0.90, sonnet_min=0.70)
    assert route_by_cosine(0.95, bands) == "haiku"
    assert route_by_cosine(0.90, bands) == "haiku"  # inclusive


def test_route_by_cosine_uncertain_band_routes_to_sonnet() -> None:
    bands = CosineBands(haiku_min=0.90, sonnet_min=0.70)
    assert route_by_cosine(0.80, bands) == "sonnet"
    assert route_by_cosine(0.70, bands) == "sonnet"  # inclusive lower edge


def test_route_by_cosine_too_low_is_manual_not_dismiss() -> None:
    # Below sonnet_min: skip the LLM for this room. NOT a dismiss — a reshoot of
    # the same property must never be dropped on a low cosine.
    bands = CosineBands(haiku_min=0.90, sonnet_min=0.70)
    assert route_by_cosine(0.50, bands) == "manual"


def test_route_by_cosine_missing_embedding_defaults_to_sonnet() -> None:
    # No stored embedding -> use the precise model, never silently weaken.
    bands = CosineBands(haiku_min=0.90, sonnet_min=0.70)
    assert route_by_cosine(None, bands) == "sonnet"


# ---- Stage 4b cosine tier: model routing + the no-dismiss manual skip -------


def test_cosine_tier_routes_high_cosine_to_haiku(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: {"data": {"merge_group_id": "g"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    seen: list[str | None] = []

    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list,
                model: str | None = None) -> dict:
        seen.append(model)
        return {"verdict": "High", "rationale": "x"}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=compare,
        cosine_fn=lambda ids_a, ids_b: 0.95,                 # high -> Haiku band
        bands=CosineBands(haiku_min=0.90, sonnet_min=0.70),
        model_for={"haiku": "claude-haiku-x", "sonnet": None},
        max_vision_calls=10,
    )
    assert seen == ["claude-haiku-x"]
    assert stats["routed_haiku"] == 1
    assert stats["clip_cosine_calls"] == 1
    assert stats["auto_visual"] == 1


def test_cosine_tier_low_cosine_skips_room_and_queues_not_dismiss(
    monkeypatch: Any,
) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list,
                model: str | None = None) -> dict:
        raise AssertionError("compare must NOT run for a manual-routed (low-cosine) room")

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=compare,
        cosine_fn=lambda ids_a, ids_b: 0.40,                 # below sonnet_min -> 'manual'
        bands=CosineBands(haiku_min=0.90, sonnet_min=0.70),
        model_for={"haiku": "h", "sonnet": None},
        max_vision_calls=10,
    )
    # A low cosine skips the LLM for that room but NEVER dismisses (protects a
    # reshoot of the same property) — the pair queues for the operator.
    assert stats["auto_visual"] == 0
    assert stats["auto_dismissed"] == 0
    assert stats["queued"] == 1


def test_pair_audit_records_a_merge(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: {"data": {"merge_group_id": "g"}},
    )
    _force_phash_merge(monkeypatch, eng)  # two cross-portal rows that pHash-merge
    conn = _FakeConn([_row(1, 101), _row(2, 102, source="bazos")])
    audit: list[dict[str, Any]] = []
    stats = eng.run_engine(conn, audit=audit, max_vision_calls=0)
    assert stats["auto_phash"] == 1
    rec = [r for r in audit if r["stage"] == "phash"]
    assert len(rec) == 1
    assert rec[0]["outcome"] == "merged"
    assert rec[0]["left_sreality_id"] in (1, 2)
    # The unified audit row carries the undo handle + provenance + factor detail.
    assert rec[0]["source"] == "engine"
    assert rec[0]["merge_group_id"] == "g"
    assert rec[0]["detail"]["reason"] == "image_phash"
    assert rec[0]["detail"]["stage"] == "phash"


def test_pair_audit_does_not_record_queued_pairs(monkeypatch: Any) -> None:
    # A queued pair IS a candidate (its factor detail lives in markers_matched), so it
    # must NOT also land in the terminal-decision audit (that was the duplicate-row bug).
    import scripts.dedup_engine as eng
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)
    monkeypatch.setattr(eng, "_enqueue_candidate", lambda *a, **k: None)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list,
                model: str | None = None) -> dict:
        return {"verdict": "Medium", "rationale": "unsure"}  # inconclusive -> queue

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    audit: list[dict[str, Any]] = []
    stats = eng.run_engine(
        conn, classify_fn=classify, compare_fn=compare, audit=audit,
        max_vision_calls=10,
    )
    assert stats["queued"] == 1
    assert audit == []


def test_pair_audit_records_visual_factors(monkeypatch: Any) -> None:
    # A visual merge audit carries the room/verdict/rationale + the photo-similarity
    # signal the operator tunes (so Decision history can render it).
    import scripts.dedup_engine as eng
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw: {"data": {"merge_group_id": "v"}},
    )
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)

    def classify(sid: int) -> dict:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    def compare(a: int, b: int, room: str, ids_a: list, ids_b: list,
                model: str | None = None) -> dict:
        return {"verdict": "High", "rationale": "same tiles"}

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    audit: list[dict[str, Any]] = []
    eng.run_engine(conn, classify_fn=classify, compare_fn=compare, audit=audit,
                   max_vision_calls=10)
    rec = [r for r in audit if r["stage"] == "visual"]
    assert len(rec) == 1 and rec[0]["outcome"] == "merged"
    d = rec[0]["detail"]
    assert d["verdict"] == "High" and d["room_type"] == "kitchen"
    assert d["rationale"] == "same tiles" and d["reason"] == "visual_match"
    assert d["phash_pairs"] == 0 and d["phash_threshold"] == 6


def test_pair_audit_is_opt_in_no_records_when_none() -> None:
    # audit defaults to None -> the hot loop appends nothing (tests/engine unaffected).
    import scripts.dedup_engine as eng
    conn = _FakeConn([_row(1, 101), _row(2, 102, source="bazos")])
    # Should not raise even though no audit sink is provided.
    eng.run_engine(conn, max_vision_calls=0)


# --- PR-B: probe memoization, group-batched pHash, dismissal treadmill -------

def test_probe_cache_clip_incomplete_queries_each_listing_once() -> None:
    """_clip_incomplete_any memoizes per LISTING: across a group's pairs, each listing
    hits the DB once (the audit measured per-pair re-probing as the dirty lane's cost
    floor). Only not-yet-cached ids are queried."""
    import scripts.dedup_engine as eng

    calls: list[list[int]] = []

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): calls.append(list(params[0]))
        def fetchall(self): return [(s,) for s in calls[-1] if s == 2]  # sid 2 incomplete

    class _Conn:
        def cursor(self): return _C()

    cache = eng._ProbeCache()
    assert eng._clip_incomplete_any(_Conn(), [1, 2], "m", cache) is True
    assert eng._clip_incomplete_any(_Conn(), [1, 3], "m", cache) is False
    assert eng._clip_incomplete_any(_Conn(), [2, 3], "m", cache) is True  # pure cache hit
    # sid 1,2 queried in call one; only sid 3 in call two; call three hit the cache.
    assert calls == [[1, 2], [3]]


def test_phash_pairs_cached_batches_group_once() -> None:
    """First lookup for a (group, profile) batches the WHOLE group's counts in one round
    trip; later pairs are cache hits; absent pairs read 0."""
    import scripts.dedup_engine as eng

    executed: list[str] = []

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): executed.append(" ".join(sql.split()))
        def fetchall(self): return [(10, 20, 3)]  # only pair (10,20) has matches

    class _Conn:
        def cursor(self): return _C()

    cache = eng._ProbeCache()
    group = (10, 20, 30)
    assert eng._phash_pairs_cached(_Conn(), 10, 20, (), None,
                                   cache=cache, group_sids=group) == 3
    assert eng._phash_pairs_cached(_Conn(), 20, 30, (), None,
                                   cache=cache, group_sids=group) == 0  # absent -> 0
    assert eng._phash_pairs_cached(_Conn(), 30, 10, (), None,
                                   cache=cache, group_sids=group) == 0
    assert len(executed) == 1 and "GROUP BY 1, 2" in executed[0]
    # A DIFFERENT exclusion profile re-batches (byt excludes exteriors/renders).
    eng._phash_pairs_cached(_Conn(), 10, 20, ("garden",), 0.95,
                            cache=cache, group_sids=group)
    assert len(executed) == 2


def test_phash_pairs_cached_falls_back_per_pair_without_group() -> None:
    """No group context (tests / standalone callers) -> the per-pair query, unchanged."""
    import scripts.dedup_engine as eng

    executed: list[str] = []

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): executed.append(" ".join(sql.split()))
        def fetchone(self): return (2,)

    class _Conn:
        def cursor(self): return _C()

    assert eng._phash_pairs_cached(_Conn(), 1, 2, (), None,
                                   cache=eng._ProbeCache(), group_sids=None) == 2
    assert "GROUP BY" not in executed[0]


def test_resolve_pair_skips_prior_dismissed_without_new_evidence() -> None:
    """A pair the engine already dismissed is SKIPPED on scoped runs unless either side
    gained photo evidence since — the 5.8x dismissal-treadmill fix, recall-preserving."""
    import scripts.dedup_engine as eng

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if "max(clip_tagged_at)" in s:
                self._one = ("2026-07-01",)  # each listing's latest photo evidence
            elif "EXISTS" in s:
                self._one = (False,)
            else:
                self._one = (0,)  # pHash count / site-plan flags
        def fetchone(self): return self._one
        def fetchall(self): return []

    class _Conn:
        def cursor(self): return _C()

    a = _key(1, pid=101)
    b = _key(2, pid=102)
    # Dismissed AFTER the evidence timestamp -> no new evidence -> skip.
    ctx = eng._RunContext(stats={"rejected": 0},
                          dismissed_prior={(101, 102): "2026-07-02"})
    eng.resolve_pair(_Conn(), a, b, street_key="id:42", ctx=ctx)
    assert ctx.stats.get("skipped_prior_dismissed") == 1
    assert ctx.stats.get("clip_deferred") is None  # exited before the probe chain

    # Evidence NEWER than the dismissal -> the pair is re-decided (falls through to the
    # visual stage and counts as considered; free-mode skip keeps the fake conn write-free).
    ctx2 = eng._RunContext(stats={"rejected": 0, "pairs_considered": 0, "queued": 0,
                                  "auto_dismissed": 0, "floor_plan_deferred": 0,
                                  "skipped_unresolved": 0},
                           dismissed_prior={(101, 102): "2026-06-30"},
                           enqueue_unresolved=False)
    eng.resolve_pair(_Conn(), a, b, street_key="id:42", ctx=ctx2)
    assert ctx2.stats.get("skipped_prior_dismissed") is None
    assert ctx2.stats["pairs_considered"] == 1


def test_resolve_retired_follows_chain_within_one_run() -> None:
    """A property_id an EARLIER merge in this same run already retired resolves through
    ctx.retired_to_survivor — including a multi-hop chain (A retired into B, B itself later
    retired into C, all within one run) — the same-run re-probe race fix."""
    import scripts.dedup_engine as eng

    ctx = eng._RunContext(stats={})
    ctx.retired_to_survivor[101] = 102
    ctx.retired_to_survivor[102] = 103  # a second, later merge this run retired 102 too

    a = _key(1, pid=101)
    resolved = eng._resolve_retired(a, ctx)
    assert resolved.property_id == 103
    assert resolved.sreality_id == 1  # every other field is untouched

    # Not in the chain -> returned unchanged.
    b = _key(2, pid=999)
    assert eng._resolve_retired(b, ctx).property_id == 999

    # No property_id at all -> returned as-is (no-op).
    c = ListingKey(sreality_id=3, property_id=None, source="sreality",
                   street_key="id:42", disposition="2+kk", house_number="10",
                   floor=3, area_m2=60.0)
    assert eng._resolve_retired(c, ctx) is c


def test_merge_pair_records_retired_to_survivor_on_ctx(monkeypatch: Any) -> None:
    """A successful _merge_pair, given a ctx, records retired->survivor on it — the
    bookkeeping a LATER pair in the same run resolves through (see _resolve_retired).
    A skipped (MergeError) merge records nothing."""
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "merge_properties",
                         lambda *a, **k: {"data": {"merge_group_id": "g1"}})
    a = _key(1, pid=101)
    b = _key(2, pid=102)
    ctx = eng._RunContext(stats={})
    mg = eng._merge_pair(None, a, b, "phash_single", {"confidence": 0.97}, ctx=ctx)
    assert mg == "g1"
    assert ctx.retired_to_survivor == {102: 101}  # survivor = the smaller (older) id

    def _raise(*_a: Any, **_k: Any) -> Any:
        raise eng.MergeError("already merged")

    monkeypatch.setattr(eng, "merge_properties", _raise)
    ctx2 = eng._RunContext(stats={})
    assert eng._merge_pair(None, a, b, "phash_single", {"confidence": 0.97}, ctx=ctx2) is None
    assert ctx2.retired_to_survivor == {}

    # No ctx passed (existing callers, e.g. scripts/submit_dedup_batch.py) -> unchanged behavior.
    monkeypatch.setattr(eng, "merge_properties",
                         lambda *a, **k: {"data": {"merge_group_id": "g2"}})
    assert eng._merge_pair(None, a, b, "phash_single", {"confidence": 0.97}) == "g2"


def test_seen_property_pairs_discarded_on_defer_allows_retry() -> None:
    """A property pair whose FIRST-tried listing-pair representative DEFERS (e.g. one side's
    CLIP tagging is still pending) is NOT permanently blocked for the rest of the run — a
    SECOND listing pair representing the SAME property pair (a multi-portal cluster where
    several duplicates already share a property) gets a fresh chance. Before the fix,
    ctx.seen_property_pairs marked the property pair 'seen' unconditionally, so the second
    representative silently never reached the clip-readiness check at all."""
    import scripts.dedup_engine as eng

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): self._ids = params[0]
        def fetchall(self): return [(sid,) for sid in self._ids]  # every id incomplete

    class _Conn:
        def cursor(self): return _C()

    ctx = eng._RunContext(stats={"clip_deferred": 0}, clip_model="ViT-B/32")
    a1, b1 = _key(1, pid=101), _key(2, pid=102)
    a2, b2 = _key(3, pid=101), _key(4, pid=102)  # a different pair, same property ids

    eng.resolve_pair(_Conn(), a1, b1, street_key="id:42", ctx=ctx)
    assert ctx.stats["clip_deferred"] == 1
    assert (101, 102) not in ctx.seen_property_pairs  # discarded, not left "seen"

    eng.resolve_pair(_Conn(), a2, b2, street_key="id:42", ctx=ctx)
    assert ctx.stats["clip_deferred"] == 2  # the second representative was actually tried


def test_resolve_candidates_stamps_first_engine_decision_at_write_once() -> None:
    """_resolve_candidates (merge/dismiss terminal outcomes) also stamps the write-once
    first_engine_decision_at — a merge or dismissal can be a pair's VERY FIRST engine
    look, so this can't be left to _stamp_engine_looked alone (which only ever sees
    pairs that stay proposed)."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        @property
        def rowcount(self): return 1

    class _Conn:
        def cursor(self): return _C()

    n = eng._resolve_candidates(_Conn(), {(1, 2)}, "merged")
    assert n == 1
    assert "SET status = %s, reviewed_at = now()" in captured["sql"]
    assert ("first_engine_decision_at = coalesce(c.first_engine_decision_at, now())"
            in captured["sql"])
    assert captured["params"] == ("merged", [1], [2])
    assert eng._resolve_candidates(_Conn(), set(), "merged") == 0


def test_record_auto_dismissed_inserts_markers() -> None:
    """finalize()'s _record_auto_dismissed writes insert-if-absent dismissed candidate
    rows for verdict-backed dismissals only (the consult's durable negative decision)."""
    import scripts.dedup_engine as eng

    captured: dict[str, Any] = {}

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None):
            captured["sql"] = " ".join(sql.split()); captured["params"] = params
        @property
        def rowcount(self): return 2

    class _Conn:
        def cursor(self): return _C()

    n = eng._record_auto_dismissed(_Conn(), {(1, 2), (3, 4)}, "street_disposition")
    assert n == 2
    # Re-dismissal refreshes the settled timestamp (guarded to dismissed rows), so a
    # once-reopened pair doesn't read as perpetually "fresh" and treadmill again.
    assert "DO UPDATE SET reviewed_at = now()" in captured["sql"]
    assert "status = 'dismissed'" in captured["sql"]
    # Session 5: a brand-new row (no prior candidate ever proposed) stamps its first
    # engine look at INSERT time; the ON CONFLICT arm coalesces defensively (write-once).
    assert "first_engine_decision_at" in captured["sql"]
    assert ("coalesce( property_identity_candidates.first_engine_decision_at, now())"
            in captured["sql"])
    assert eng._record_auto_dismissed(_Conn(), set(), "street_disposition") == 0


def test_write_pair_audit_dedupes_recent_identical_records() -> None:
    """A dismissal identical to one logged within the window is NOT re-appended (the
    audit logs decisions, not run cadence); novel records still insert."""
    import scripts.dedup_engine as eng

    inserted: list[Any] = []

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def execute(self, sql, params=None): self._sql = " ".join(sql.split())
        def executemany(self, sql, rows): inserted.extend(rows)
        def fetchall(self):  # pair (1,2) phash/engine dismissal already logged
            return [(1, 2, "phash", "engine")]

    class _Conn:
        def cursor(self): return _C()

    rec = {
        "left_sreality_id": 1, "right_sreality_id": 2, "left_property_id": 101,
        "right_property_id": 102, "category_main": "byt", "stage": "phash",
        "outcome": "dismissed", "source": "engine", "detail": {},
    }
    novel = {**rec, "left_sreality_id": 5, "right_sreality_id": 6}
    # A re-MERGE of the same pair must ALWAYS land (its fresh merge_group_id is the
    # operator's only undo handle after an unmerge) — only dismissals dedupe.
    remerge = {**rec, "outcome": "merged", "merge_group_id": "G2"}
    eng._write_pair_audit(_Conn(), "RUN_AT", [rec, novel, remerge])
    assert len(inserted) == 2
    assert {r[1] for r in inserted} == {5, 1}          # novel dismissal + the re-merge
    # left/right_listing_id repeat their sreality_id at the mirrored position (R2 dual-write).
    assert all(r[1] == r[2] and r[3] == r[4] for r in inserted)
    assert any(r[9] == "merged" for r in inserted)     # the merged record landed


def test_enqueue_candidate_reopen_valve() -> None:
    """reopen=True (consult re-decided on fresh evidence) re-proposes an engine-dismissed
    row so a queue outcome reaches the operator; operator dismissals stay respected; the
    default path keeps DO NOTHING so mass re-decides can't bulk-reopen settled pairs."""
    import scripts.dedup_engine as eng

    captured: list[str] = []

    class _Ctx:
        def __enter__(self): return self
        def __exit__(self, *a): return None

    class _C(_Ctx):
        def execute(self, sql, params=None): captured.append(" ".join(sql.split()))

    class _Conn:
        def cursor(self): return _C()
        def transaction(self): return _Ctx()

    a = _key(1, pid=101)
    b = _key(2, pid=102)
    eng._enqueue_candidate(_Conn(), a, b, {"tier": "street_disposition"}, reopen=True)
    assert "SET status = 'proposed', reviewed_at = NULL" in captured[-1]
    assert "reviewed_action IS DISTINCT FROM 'operator'" in captured[-1]
    eng._enqueue_candidate(_Conn(), a, b, {"tier": "street_disposition"})
    assert "DO NOTHING" in captured[-1]


def test_scoped_runs_skip_the_market_gauge_scan() -> None:
    """Scoped runs (dirty/candidates) must NOT pay the ~9s full-table eligibility
    aggregate — they write NULL gauges (migration 265); only the unscoped full scan
    measures the market. Dashboards read gauges from full-scan rows."""
    import scripts.dedup_engine as eng

    conn = _FakeConn([])
    stats = eng.run_engine(conn, max_vision_calls=0,
                           only_groups_with_property_ids=set())
    assert stats["eligible"] is None and stats["flagged_location"] is None
    assert not any("count(*) FILTER" in s for s in conn.executed)

    conn2 = _FakeConn([])
    stats2 = eng.run_engine(conn2, max_vision_calls=0)  # unscoped full scan
    assert stats2["eligible"] == 4  # the fake's gauge row
    assert any("count(*) FILTER" in s for s in conn2.executed)


# --- Engine-fed batch deferral (§4.1): _resolve_visual + resolve_pair ------
#
# These exercise the 'batch_pending' defer outcome the deferring fn builders
# produce (scripts.dedup_engine._build_classify_fn/_build_compare_fn/
# _build_site_plan_fn with defer_to_batch=True — tested end-to-end via
# toolkit.dedup_batch_defer in test_dedup_batch_defer.py). Here the fn
# closures are hand-rolled to isolate _resolve_visual's / resolve_pair's own
# handling of the {"deferred": True} sentinel from the spooling mechanics.

def test_resolve_visual_defers_when_classify_is_deferred() -> None:
    import scripts.dedup_engine as eng

    a, b = _key(1, category_main="dum"), _key(2, category_main="dum")
    calls: list[int] = []

    def classify_fn(sid: int) -> dict[str, Any]:
        calls.append(sid)
        if sid == a.sreality_id:
            return {"deferred": True}
        return {"data": {"images": [{"image_id": 99, "room_type": "kitchen"}]}}

    outcome = eng._resolve_visual(
        None, a, b, classify_fn=classify_fn, compare_fn=None, site_plan_fn=None,
        vision_budget=[10], max_room_attempts=4,
    )
    assert outcome == {"action": "defer", "reason": "batch_pending"}
    # both sides are always probed (no short-circuit before the deferred check).
    assert calls == [a.sreality_id, b.sreality_id]


def test_resolve_visual_defers_when_site_plan_is_deferred() -> None:
    import scripts.dedup_engine as eng

    a, b = _key(1, category_main="dum"), _key(2, category_main="dum")

    def classify_fn(sid: int) -> dict[str, Any]:
        return {"data": {"images": [{"image_id": sid, "room_type": "site_plan"}]}}

    def site_plan_fn(
        a_id: int, b_id: int, ids_a: list[int], ids_b: list[int], family: str | None = None,
    ) -> dict[str, Any]:
        return {"deferred": True}

    budget = [10]
    outcome = eng._resolve_visual(
        None, a, b, classify_fn=classify_fn, compare_fn=None, site_plan_fn=site_plan_fn,
        vision_budget=budget, max_room_attempts=4,
    )
    assert outcome == {"action": "defer", "reason": "batch_pending"}
    assert budget == [10]  # a deferred call never consumes the vision budget


def test_resolve_visual_defers_on_first_room_and_does_not_try_others() -> None:
    import scripts.dedup_engine as eng

    a, b = _key(1, category_main="dum"), _key(2, category_main="dum")

    def classify_fn(sid: int) -> dict[str, Any]:
        return {"data": {"images": [
            {"image_id": sid * 10 + 1, "room_type": "kitchen"},
            {"image_id": sid * 10 + 2, "room_type": "living_room"},
        ]}}

    tried_rooms: list[str] = []

    def compare_fn(a_id: int, b_id: int, room_type: str, ids_a: list[int], ids_b: list[int],
                   model: str | None = None) -> dict[str, Any]:
        tried_rooms.append(room_type)
        return {"deferred": True}

    budget = [10]
    outcome = eng._resolve_visual(
        None, a, b, classify_fn=classify_fn, compare_fn=compare_fn, site_plan_fn=None,
        vision_budget=budget, max_room_attempts=4,
    )
    assert outcome["action"] == "defer"
    assert outcome["reason"] == "batch_pending"
    assert len(tried_rooms) == 1  # stops at the first deferred room (--warm-rooms=1 parity)
    assert budget == [10]  # no budget spent on a deferred (not cache-miss-paid) call


def test_resolve_pair_defer_splits_stats_by_reason(monkeypatch: Any) -> None:
    """resolve_pair must NOT lump a fresh batch_pending defer into
    floor_plan_deferred — the success-gate measurement needs the two counted
    separately (spool growth vs the pre-existing floor-plan-gate wait)."""
    import scripts.dedup_engine as eng

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 0)  # skip pHash -> visual

    def classify_fn(sid: int) -> dict[str, Any]:
        return {"data": {"images": [{"image_id": sid * 10 + 1, "room_type": "kitchen"}]}}

    def compare_fn(a_id: int, b_id: int, room_type: str, ids_a: list[int], ids_b: list[int],
                   model: str | None = None) -> dict[str, Any]:
        return {"deferred": True}

    stats = eng.run_engine(
        conn, classify_fn=classify_fn, compare_fn=compare_fn, floor_plan_fn=None,
        max_vision_calls=10,
    )
    assert stats.get("batch_deferred", 0) == 1
    assert stats.get("floor_plan_deferred", 0) == 0


# --- Gate 2: listing-identity repoint (sreality_id -> surrogate listing_id) ---------
# Post-Gate-2 the non-sreality portals insert sreality_id=NULL. The engine must key every
# identity decision on the NOT-NULL surrogate listing_id, never sreality_id. Pre-fix:
#   * classify_pair compared a.sreality_id == b.sreality_id -> None==None is True, so two
#     DISTINCT NULL-sreality listings wrongly rejected as "same_listing" (silent wrong answer);
#   * the loaders did bare int(r[0]) on sreality_id -> int(None) crashes the whole pass;
#   * the group pHash batch sorted {m.sreality_id ...} -> sorted({...None...}) crashes;
#   * every image probe read images.sreality_id (NULL for these rows) -> the pHash /
#     floor-plan / site-plan / readiness conservatism layers failed OPEN (auto-merge).
# images.listing_id is a NOT-NULL FK to listings.id (verified live), so the repoint is
# behaviour-identical for a sreality row and correct for a NULL-sreality row.

def _null_sreality_key(*, pid: int, lid: int, source: str = "bazos",
                       street_key: str = "id:42|d:2+kk") -> ListingKey:
    """A post-Gate-2 non-sreality ListingKey: sreality_id=None, distinct surrogate listing_id."""
    return ListingKey(
        sreality_id=None, property_id=pid, source=source, street_key=street_key,
        disposition="2+kk", house_number=None, floor=3, area_m2=60.0,
        category_type="prodej", category_main="byt", street_id=42, listing_id=lid,
    )


def test_classify_pair_distinct_null_sreality_not_same_listing() -> None:
    # Two distinct non-sreality listings sharing street+disposition -> a candidate, NOT a
    # 'same_listing' reject. Pre-fix: None==None -> reject "same_listing".
    a = _null_sreality_key(pid=101, lid=480001)
    b = _null_sreality_key(pid=102, lid=480002, source="idnes")
    d = classify_pair(a, b)
    assert d.detail != "same_listing"
    assert d.action == "candidate"


def test_classify_pair_same_surrogate_still_rejects_same_listing() -> None:
    # The guard still fires on a genuine self-pair, identified by the surrogate PK (not pid).
    a = _null_sreality_key(pid=101, lid=480001)
    b = _null_sreality_key(pid=999, lid=480001)  # same listing_id, different property_id
    assert classify_pair(a, b).detail == "same_listing"


def _null_sreality_geo_key(*, pid: int, lid: int, cat: str = "dum",
                           source: str = "bazos", disp: str = "") -> ListingKey:
    return ListingKey(
        sreality_id=None, property_id=pid, source=source, street_key="geo:cell",
        disposition=disp, house_number=None, floor=(3 if disp else None), area_m2=120.0,
        category_type="prodej", category_main=cat, street_id=None,
        lat=50.10064, lng=14.53742, price_czk=5_950_000, listing_id=lid,
    )


def test_classify_geo_pair_distinct_null_sreality_not_same_listing() -> None:
    a = _null_sreality_geo_key(pid=101, lid=480001)
    b = _null_sreality_geo_key(pid=102, lid=480002, source="idnes")
    d = classify_geo_pair(a, b, profile_for("dum"))
    assert d.detail != "same_listing"       # pre-fix: None==None -> reject "same_listing"
    assert d.action == "auto_merge"          # identical coord+area+price dum -> strong


def test_classify_byt_geo_pair_distinct_null_sreality_not_same_listing() -> None:
    a = _null_sreality_geo_key(pid=101, lid=480001, cat="byt", disp="2+kk")
    b = _null_sreality_geo_key(pid=102, lid=480002, cat="byt", disp="2+kk", source="idnes")
    d = classify_byt_geo_pair(a, b, profile_for("byt"))
    assert d.detail != "same_listing"
    assert d.action == "candidate" and d.reason == "byt_geo"


def test_load_eligible_tolerates_null_sreality_id() -> None:
    # Pre-fix: sreality_id=int(r[0]) -> int(None) TypeError kills the whole load.
    import scripts.dedup_engine as eng
    conn = _FakeConn([_row(None, 101, lid=480001), _row(None, 102, lid=480002)])
    keys = eng._load_eligible(conn)
    assert keys, "NULL-sreality rows were dropped from the eligible load"
    assert all(k.sreality_id is None for k in keys)
    assert {k.listing_id for k in keys} == {480001, 480002}


def test_load_geo_eligible_tolerates_null_sreality_id() -> None:
    import scripts.dedup_engine as eng
    conn = _FakeConn([], geo_rows=[_geo_row(None, 101, lid=480001),
                                   _geo_row(None, 102, lid=480002)])
    keys = eng._load_geo_eligible(conn, rung="geo")
    assert {k.listing_id for k in keys} == {480001, 480002}
    assert all(k.sreality_id is None for k in keys)


def test_gate2_image_probes_query_listing_id_not_sreality_id() -> None:
    # Every per-listing / per-pair image probe must read images.listing_id (the NOT-NULL
    # surrogate) and bind the fed listing_id — NOT images.sreality_id. Pre-fix each read
    # sreality_id, so post-Gate-2 (sreality_id NULL) the conservatism layers returned empty
    # ("ready" / "no plan" / "no shared render") and failed OPEN into a merge.
    import scripts.dedup_engine as eng

    class _Cur:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self.rows = rows
            self.calls: list[tuple[str, Any]] = []

        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

        def execute(self, sql: str, params: Any = None) -> None:
            self.calls.append((" ".join(sql.split()), params))

        def fetchone(self) -> tuple[Any, ...] | None:
            return self.rows[0] if self.rows else None

        def fetchall(self) -> list[tuple[Any, ...]]:
            return self.rows

    class _Conn:
        def __init__(self, rows: list[tuple[Any, ...]]) -> None:
            self._cur = _Cur(rows)

        def cursor(self) -> _Cur:
            return self._cur

    LID_A, LID_B = 480001, 480002

    def cap(fn: Any, rows: list[tuple[Any, ...]], *args: Any, **kw: Any) -> tuple[str, Any]:
        conn = _Conn(rows)
        fn(conn, *args, **kw)
        return conn._cur.calls[0]

    sql, params = cap(eng._floor_plan_image_ids, [], LID_A)
    assert "i.listing_id" in sql and "sreality_id" not in sql and params["lid"] == LID_A

    sql, params = cap(eng._both_have_site_plan, [(False,)], LID_A, LID_B)
    assert "i.listing_id" in sql and "sreality_id" not in sql
    assert params["a"] == LID_A and params["b"] == LID_B

    sql, params = cap(eng._phash_identical_pairs, [(0,)], LID_A, LID_B)
    assert "ia.listing_id" in sql and "ib.listing_id" in sql and "sreality_id" not in sql
    assert params["a"] == LID_A and params["b"] == LID_B

    sql, _ = cap(eng._phash_group_counts, [], [LID_A, LID_B, 480003])
    assert "ia.listing_id" in sql and "ib.listing_id" in sql and "sreality_id" not in sql

    sql, params = cap(eng._high_render_image_ids, [], LID_A, LID_B, 0.9)
    assert "i.listing_id IN" in sql and "sreality_id" not in sql

    sql, _ = cap(eng._clip_incomplete, [], [LID_A], "m")
    assert "i.listing_id = s.lid" in sql and "sreality_id" not in sql

    sql, _ = cap(eng._downloads_incomplete, [], [LID_A])
    assert "i.listing_id = s.lid" in sql and "sreality_id" not in sql

    conn = _Conn([(None,)])
    eng._last_evidence_at(conn, LID_A, eng._ProbeCache())
    sql, params = conn._cur.calls[0]
    assert "WHERE listing_id" in sql and "sreality_id" not in sql and params == (LID_A,)


def test_run_engine_merges_two_null_sreality_listings(monkeypatch: Any) -> None:
    # End-to-end: two NULL-sreality listings sharing street+disposition with a pHash match
    # MERGE. Exercises the loader (no int(None) crash), classify_pair (no false same_listing),
    # the group_sid batch (no None-sort crash) and the probe feed together.
    import scripts.dedup_engine as eng

    merges: list[str] = []
    monkeypatch.setattr(
        eng, "merge_properties",
        lambda conn, *, survivor_id, retired_id, reason, **kw:
            merges.append(reason) or {"data": {"merge_group_id": "g"}})
    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    conn = _FakeConn([
        _row(None, 101, hn=None, source="bazos", lid=480001),
        _row(None, 102, hn=None, source="idnes", lid=480002),
    ])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, max_vision_calls=10)
    assert stats["auto_phash"] == 1
    assert merges == ["image_phash"]


def test_run_engine_group_sid_batch_keys_on_listing_id(monkeypatch: Any) -> None:
    # A 3-member group with a MIXED sreality_id set (5, None, None) must batch the pHash probe
    # on the surrogate listing_id. Pre-fix: sorted({5, None, None}) -> TypeError. The loader is
    # bypassed to isolate the group_sid construction from the int(None) loader crash.
    import scripts.dedup_engine as eng

    keys = [
        _null_sreality_key(pid=101, lid=480001, source="bazos"),
        ListingKey(sreality_id=5, property_id=102, source="sreality",
                   street_key="id:42|d:2+kk", disposition="2+kk", house_number=None,
                   floor=3, area_m2=60.0, category_type="prodej", category_main="byt",
                   street_id=42, listing_id=480002),
        _null_sreality_key(pid=103, lid=480003, source="idnes"),
    ]
    monkeypatch.setattr(eng, "_load_eligible", lambda conn, **k: keys)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        eng, "_phash_group_counts",
        lambda conn, ids, *a, **k: captured.__setitem__("ids", list(ids)) or {})
    monkeypatch.setattr(eng, "_phash_group_distinctive", lambda conn, ids, *a, **k: set())
    monkeypatch.setattr(eng, "merge_properties",
                        lambda *a, **k: {"data": {"merge_group_id": "g"}})
    eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None, max_vision_calls=10)
    assert captured["ids"] == [480001, 480002, 480003]
