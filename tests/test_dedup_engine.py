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
    classify_geo_pair,
    classify_pair,
    decide_phash_fastpath,
    decide_visual_dismiss,
    disposition_compatible,
    distinctive_rooms_for,
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
    street_id: int | None = None,
) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid if pid is not None else sid,
        source=source, street_key=street, disposition=disp,
        house_number=hn, floor=floor, area_m2=area, description=description,
        category_type=category_type, category_main=category_main,
        street_id=street_id,
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


def test_only_houses_geo_auto_merge_and_always_behind_dev_guard() -> None:
    # Operator policy: houses may auto-merge (coord+area+price, validated 83.5%
    # same-price) but ONLY with the same-development guard; land/commercial/other
    # are queue-only (weaker signal) until a human confirms.
    dum = profile_for("dum")
    assert dum.geo_auto_merge_allowed is True
    assert dum.requires_development_guard is True
    for fam in ("pozemek", "komercni", "ostatni"):
        assert profile_for(fam).geo_auto_merge_allowed is False, fam


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
    desc: str | None = None,
) -> ListingKey:
    return ListingKey(
        sreality_id=sid, property_id=pid, source=source, street_key="geo:cell",
        disposition="", house_number=hn, floor=None, area_m2=area, description=desc,
        category_type=ct, category_main=cat, street_id=None, lat=lat, lng=lng,
        price_czk=price,
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
    assert stats["skipped_same_source"] == 0
    assert stats["pairs_considered"] == 1


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


def test_run_engine_geo_skips_oversized_cell(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng
    members = [_gk(i, 100 + i) for i in range(1, eng.MAX_GEO_GROUP_SIZE + 3)]  # one oversized cell
    monkeypatch.setattr(eng, "_load_geo_eligible", lambda conn, **k: members)
    monkeypatch.setattr(eng, "_enqueue_candidate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("oversized cell must skip")))
    stats = eng.run_engine(_FakeConn([]), classify_fn=None, compare_fn=None,
                           max_vision_calls=0, geo=True)
    assert stats["pairs_considered"] == 0


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
        elif "FROM listings l" in s and "l.street IS NOT NULL" in s:
            self._rows = list(self._conn.eligible_rows)
        elif "count(*)" in s and "JOIN properties pl" in s:
            self._rows = [(self._conn.stale_count,)]  # _reconcile_stale_candidates count
        elif "EXISTS (SELECT 1 FROM images ia" in s:
            self._rows = [(False,)]  # _phash_distinctive_match default (monkeypatch for a match)
        elif "FROM images ia JOIN images ib" in s:
            self._rows = [(0,)]  # _phash_identical_pairs default (tests monkeypatch when needed)
        elif "FROM images i WHERE i.sreality_id" in s and "storage_path IS NOT NULL" in s:
            self._rows = []  # _floor_plan_image_ids default (no floor plan -> gate passes)
        elif "image_room_classifications" in s:
            self._rows = [(False,)]  # _both_have_site_plan default (CLIP-OR-LLM query)
        elif "UPDATE property_identity_candidates" in s:
            self._conn.resolved.append((s, params))  # reconcile / _resolve_candidates
            self._rows = []
        elif "INSERT INTO property_identity_candidates" in s:
            self._conn.enqueued.append(params)
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, eligible_rows: list[tuple[Any, ...]], stale_count: int = 0) -> None:
        self.eligible_rows = eligible_rows
        self.stale_count = stale_count
        self.executed: list[str] = []
        self.enqueued: list[Any] = []
        self.resolved: list[tuple[str, Any]] = []  # reconcile + _resolve_candidates UPDATEs

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
         obec_id: int | None = 5001) -> tuple[Any, ...]:
    # matches _ELIGIBLE_SQL column order:
    # sreality_id, property_id, source, street, street_id, disposition,
    # house_number, floor, area_m2, description, category_type, category_main, obec_id
    return (sid, pid, source, street, street_id, disp, hn, floor, area,
            description, category_type, category_main, obec_id)


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

    assert stats["skipped_same_source"] == 0   # the gate is gone
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

    monkeypatch.setattr(eng, "_clip_incomplete", lambda conn, sids, model: [2])  # 2 still tagging
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

    assert stats["skipped_same_source"] == 0
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
    assert stats["skipped_same_source"] == 0  # pHash resolved it before the gate
    assert merges == ["image_phash"]


# --- #4 floor-plan validation gate (migration 234) -------------------------- #

def test_effective_vision_cap() -> None:
    import scripts.dedup_engine as eng

    # cache-only: never throttle warm reads
    assert eng._effective_vision_cap(
        free=False, cache_only=True, floor_plan_budget=0, max_vision_calls=300) == 10_000_000
    # free + a positive floor-plan budget -> the cap IS that budget (bounds inline
    # cold floor-plan checks; nothing else consumes vision in free mode)
    assert eng._effective_vision_cap(
        free=True, cache_only=False, floor_plan_budget=120, max_vision_calls=300) == 120
    # free + budget 0 -> cache-only floor_plan_fn: a large cap so a zero budget can't
    # pre-empt the gate before it reads the warm cache (the cache-only fn never makes a
    # cold call, so it can't overspend)
    assert eng._effective_vision_cap(
        free=True, cache_only=False, floor_plan_budget=0, max_vision_calls=300) == 10_000_000
    # live dispatch: the plain vision budget
    assert eng._effective_vision_cap(
        free=False, cache_only=False, floor_plan_budget=120, max_vision_calls=300) == 300


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


def test_claim_dedup_dirty_is_bounded_fifo() -> None:
    """The --dirty claim must be FIFO-bounded when a limit is passed (and unbounded only when
    not), so a tagging-backlog flood can't make an hourly run claim the whole market."""
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
    assert "ORDER BY marked_at" in sql and "LIMIT %s" in sql
    assert params == ["CUTOFF", 5000]

    captured.clear()
    eng._claim_dedup_dirty(_Conn(), "CUTOFF")  # unbounded (full sweep / reconcile use)
    sql, params = captured[-1]
    assert "LIMIT" not in sql and params == ["CUTOFF"]


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
        def fetchall(self):  # one eligible row (13 cols) -> exercises ListingKey build
            return [(1, 101, "sreality", "Hlavní", None, "2+kk", "10", 3, 60.0,
                     "desc", "prodej", "byt", 42)]

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


def test_resolve_pair_seam_standalone() -> None:
    """resolve_pair is callable standalone with a hand-built _RunContext — the exact seam
    the candidate-priority drain + the real-time per-listing path reuse (one decision tree,
    many drivers). A street_id contradiction rejects with no DB access."""
    import scripts.dedup_engine as eng
    from toolkit.dedup_engine import ListingKey

    a = ListingKey(1, 101, "sreality", "name:5001:nadrazni", "2+kk", "10", 3, 60.0, street_id=1)
    b = ListingKey(2, 102, "sreality", "name:5001:nadrazni", "2+kk", "10", 3, 60.0, street_id=2)
    ctx = eng._RunContext(stats={"rejected": 0})
    eng.resolve_pair(None, a, b, street_key="name:5001:nadrazni", ctx=ctx)
    assert ctx.stats["rejected"] == 1
    # the rejected pair is collected so the run finalize can dismiss any stale candidate
    assert (101, 102) in ctx.dismissed_pairs


def test_floor_plan_gate_branches(monkeypatch: Any) -> None:
    import scripts.dedup_engine as eng

    # neither side has a floor plan -> merge (existing path unchanged)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [])
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=None, vision_budget=[5]) == "merge"

    # exactly one side has a plan -> queue (can't compare plan-to-plan)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [9] if sid == 1 else [])
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=None, vision_budget=[5]) == "queue"

    # both have plans + a verdict available -> confirm/dismiss
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [9])
    diff = lambda a, b, ia, ib: {"verdict": "different_layout"}  # noqa: E731
    same = lambda a, b, ia, ib: {"verdict": "same_layout"}       # noqa: E731
    none = lambda a, b, ia, ib: None                             # noqa: E731 (unwarmed)
    inconc = lambda a, b, ia, ib: {"verdict": "inconclusive"}    # noqa: E731
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=diff, vision_budget=[5]) == "dismiss"
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=same, vision_budget=[5]) == "merge"
    # 'inconclusive' -> manual review by default (toggle on); off -> treat as same -> merge
    assert eng._floor_plan_gate(
        None, 1, 2, floor_plan_fn=inconc, vision_budget=[5],
        inconclusive_to_review=True) == "queue"
    assert eng._floor_plan_gate(
        None, 1, 2, floor_plan_fn=inconc, vision_budget=[5],
        inconclusive_to_review=False) == "merge"
    # both have plans but can't validate now (no fn / no budget / unwarmed) -> DEFER,
    # NOT the manual queue (the pair is automatable once the batch warms the verdict)
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=None, vision_budget=[5]) == "defer"
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=same, vision_budget=[0]) == "defer"
    assert eng._floor_plan_gate(None, 1, 2, floor_plan_fn=none, vision_budget=[5]) == "defer"
    # a COLD verdict consumes one budget unit
    budget = [3]
    eng._floor_plan_gate(None, 1, 2, floor_plan_fn=same, vision_budget=budget)
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


def test_run_engine_phash_floor_plan_one_sided_queues(monkeypatch: Any) -> None:
    # pHash would merge, but only ONE side has a floor plan -> operator queue.
    import scripts.dedup_engine as eng

    monkeypatch.setattr(eng, "_phash_identical_pairs", lambda *a, **k: 3)
    monkeypatch.setattr(eng, "_both_have_site_plan", lambda *a, **k: False)
    monkeypatch.setattr(eng, "_floor_plan_image_ids", lambda conn, sid: [sid] if sid == 1 else [])

    conn = _FakeConn([_row(1, 101, hn=None), _row(2, 102, hn=None, source="bazos")])
    stats = eng.run_engine(conn, classify_fn=None, compare_fn=None, floor_plan_fn=None, max_vision_calls=10)

    assert stats["auto_phash"] == 0
    assert stats["queued"] >= 1


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

    def site_plan(a: int, b: int, ids_a: list, ids_b: list) -> dict:
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
