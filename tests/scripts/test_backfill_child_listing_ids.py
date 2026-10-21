"""scripts.backfill_child_listing_ids — the carrier skips its predicate applies (offline).

These pins guarded the backfill from inside the dual-write parity check's tests until wave 3
deleted that check; the skips are the backfill's to honour, so they live beside it now."""

from __future__ import annotations

from scripts.backfill_child_listing_ids import _predicate
from toolkit.listing_identity import R2_CARRIERS, R2_CARRIERS_BY_TABLE


def test_properties_skips_a_property_reset_to_no_ads() -> None:
    """The recompute resets a property whose ads all moved away: both ids NULL and a count of 0,
    which only that reset writes. It names no advert on purpose; the skip names exactly it."""
    from scripts.recompute_property_stats import _NO_ADS

    carrier = R2_CARRIERS_BY_TABLE["properties"]
    assert _NO_ADS["repr_listing_id"] == _NO_ADS["repr_listing_ref_id"] == "NULL"
    assert carrier["skip"] == f"t.source_count = {_NO_ADS['source_count']}"
    assert "NOT (t.source_count = 0)" in _predicate(
        "repr_listing_ref_id", "repr_listing_id", False, carrier["skip"])


def test_carrier_skip_is_applied_by_counting_and_by_updating() -> None:
    """Skip one side but not the other and `remaining` never reaches zero, which
    re-dispatches the self-chaining backfill workflow forever."""
    for carrier in R2_CARRIERS:
        skip = carrier.get("skip")
        if not skip:
            continue
        for legacy, new in carrier["cols"]:
            for repair in (False, True):
                assert f"NOT ({skip})" in _predicate(new, legacy, repair, skip)
