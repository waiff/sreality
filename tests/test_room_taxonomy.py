"""The single-source image-tag taxonomy + family grouping is internally consistent
and is what image_classification / dedup_engine re-export."""

from __future__ import annotations

from toolkit import room_taxonomy as rt


def test_every_tag_has_a_family() -> None:
    assert set(rt.ROOM_TYPES) == set(rt.ROOM_FAMILIES)
    assert all(fam in ("interior", "exterior", "common", "plan", "other")
               for fam in rt.ROOM_FAMILIES.values())


def test_interior_set_matches_interior_family() -> None:
    interior = {t for t, f in rt.ROOM_FAMILIES.items() if f == "interior"}
    assert rt.INTERIOR_ROOM_TYPES == interior
    assert set(rt.INTERIOR_PRIORITY) == interior  # ordered form covers the same set


def test_non_interior_tags_are_exterior_common_plan_not_other() -> None:
    assert set(rt.NON_INTERIOR_TAGS) == {
        t for t, f in rt.ROOM_FAMILIES.items() if f in ("exterior", "common", "plan")
    }
    assert "other" not in rt.NON_INTERIOR_TAGS  # untagged/unknown still counts
    # shared building circulation (stairwells) is excluded from the unit-match signal
    assert "staircase_interior" in rt.NON_INTERIOR_TAGS
    assert "staircase_exterior" in rt.NON_INTERIOR_TAGS


def test_distinctive_rooms_are_interior_and_lead_priority() -> None:
    assert rt.DISTINCTIVE_ROOMS <= rt.INTERIOR_ROOM_TYPES
    assert rt.INTERIOR_PRIORITY[:2] == ("kitchen", "bathroom")  # most distinctive first


def test_full_priority_covers_every_tag_that_can_be_compared() -> None:
    # FULL_PRIORITY (non-byt) excludes only the never-compared plan/other families.
    comparable = {t for t, f in rt.ROOM_FAMILIES.items() if f in ("interior", "exterior")}
    assert set(rt.FULL_PRIORITY) == comparable


def test_reexports_match_single_source() -> None:
    from toolkit import image_classification as ic
    from toolkit import dedup_engine as de

    assert ic.ROOM_TYPES == rt.ROOM_TYPES
    assert ic.INTERIOR_ROOM_TYPES == rt.INTERIOR_ROOM_TYPES
    assert ic.SITE_PLAN_ROOM_TYPE == rt.SITE_PLAN_ROOM_TYPE
    assert de.BYT_ROOM_PRIORITY == rt.INTERIOR_PRIORITY
    assert de.ROOM_PRIORITY == rt.FULL_PRIORITY
    assert de.NON_INTERIOR_TAGS == rt.NON_INTERIOR_TAGS
    assert de.DISTINCTIVE_ROOMS == rt.DISTINCTIVE_ROOMS


# --- IMAGE_ROLE_REGISTRY (Session 5b): the per-family, per-tag role declaration that
# INTERIOR_PRIORITY/HOUSE_PRIORITY/LAND_PRIORITY/NON_INTERIOR_TAGS/DISTINCTIVE_ROOMS are
# now DERIVED from — these tests pin that the derivation is faithful and stays that way.

def test_registry_covers_every_family_and_every_tag() -> None:
    assert set(rt.IMAGE_ROLE_REGISTRY) == {"byt", "dum", "komercni", "ostatni", "pozemek"}
    for family, roles in rt.IMAGE_ROLE_REGISTRY.items():
        assert set(roles) == set(rt.ROOM_TYPES), family


def test_registry_house_families_share_one_object() -> None:
    # dum/komercni/ostatni are the SAME shape today (HOUSE_PRIORITY) — one shared dict,
    # not three independently hand-maintained copies that could silently drift apart.
    reg = rt.IMAGE_ROLE_REGISTRY
    assert reg["dum"] is reg["komercni"] is reg["ostatni"]
    assert reg["byt"] is not reg["dum"]
    assert reg["pozemek"] is not reg["dum"]


def test_priority_order_derives_from_registry_forensic_order() -> None:
    assert rt._priority_order(rt.IMAGE_ROLE_REGISTRY["byt"]) == rt.INTERIOR_PRIORITY
    assert rt._priority_order(rt.IMAGE_ROLE_REGISTRY["dum"]) == rt.HOUSE_PRIORITY
    assert rt._priority_order(rt.IMAGE_ROLE_REGISTRY["pozemek"]) == rt.LAND_PRIORITY


def test_gate_tags_are_floor_plan_and_site_plan_in_every_family() -> None:
    # Plan tags GATE (veto a would-merge) in every family; they never vote/dismiss on
    # their own — a structurally separate role from pHash/forensic evidence.
    for family, roles in rt.IMAGE_ROLE_REGISTRY.items():
        gated = {tag for tag, role in roles.items() if role.gate}
        assert gated == {"floor_plan", "site_plan"}, family


def test_dismiss_qualifying_tags_facade_flag_is_non_byt_only() -> None:
    byt = rt.IMAGE_ROLE_REGISTRY["byt"]
    dum = rt.IMAGE_ROLE_REGISTRY["dum"]
    pozemek = rt.IMAGE_ROLE_REGISTRY["pozemek"]

    # Unconditional dismiss set (flag off) == the historical DISTINCTIVE_DISMISS_ROOMS,
    # for every family — the flag only ever ADDS exterior_facade, never removes kitchen/bathroom.
    assert rt.dismiss_qualifying_tags(byt, facade_dismiss=False) == {"kitchen", "bathroom"}
    assert rt.dismiss_qualifying_tags(dum, facade_dismiss=False) == {"kitchen", "bathroom"}
    assert rt.dismiss_qualifying_tags(pozemek, facade_dismiss=False) == {"kitchen", "bathroom"}

    # Flag on: byt NEVER gains facade (a development's shared shell says nothing about
    # which unit); non-byt families do.
    assert rt.dismiss_qualifying_tags(byt, facade_dismiss=True) == {"kitchen", "bathroom"}
    assert rt.dismiss_qualifying_tags(dum, facade_dismiss=True) == {
        "kitchen", "bathroom", "exterior_facade"}
    assert rt.dismiss_qualifying_tags(pozemek, facade_dismiss=True) == {
        "kitchen", "bathroom", "exterior_facade"}


def test_phash_vote_matches_non_interior_tags_for_byt_only() -> None:
    # byt is the only family with any non-voting tags (NON_INTERIOR_TAGS); every other
    # family votes every tag in the pHash/cosine count (non-byt excludes nothing).
    for family, roles in rt.IMAGE_ROLE_REGISTRY.items():
        non_voting = {tag for tag, role in roles.items() if not role.phash_vote}
        if family == "byt":
            assert non_voting == set(rt.NON_INTERIOR_TAGS)
        else:
            assert non_voting == set()
