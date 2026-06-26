"""The single-source image-tag taxonomy + family grouping is internally consistent
and is what image_classification / dedup_engine re-export."""

from __future__ import annotations

from toolkit import room_taxonomy as rt


def test_every_tag_has_a_family() -> None:
    assert set(rt.ROOM_TYPES) == set(rt.ROOM_FAMILIES)
    assert all(fam in ("interior", "exterior", "plan", "other")
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
