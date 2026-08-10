"""S9 — the deterministic reconciler and the ledger's identity rules (03 §3.11, 00 §8).

The comparison step is pure string/geometry logic and needs no model: on the same 27
listings a small model reached 11 % major-contradiction recall with a 1 : 7 SNR, and on the
two realitymix rows it read the location correctly, sat it next to a stored pin 180 km away
in Slovakia with the whole admin hierarchy NULL, and emitted `"contradictions": []` twice.
"""

from __future__ import annotations

from location_data.resolver import core, normalize, reconciler
from location_data.resolver.version import RESOLVER_VERSION
from tests.location_data import mini_mirror as mm


def _resolve(claims, mirror=None):
    return core.resolve(
        claims, mm.context(mirror), resolver_version=RESOLVER_VERSION,
        registry_version_id=7, policy_version="v1", collision_epoch_id=11,
    )


def _run(claims, mirror=None, **kwargs):
    mirror = mirror or mm.default_mirror()
    resolution = _resolve(claims, mirror)
    return resolution, reconciler.run(
        resolution, claims, normalize.normalize_all(claims), registry=mirror, **kwargs
    )


# --------------------------------------------------------------------- identity rules


def test_the_dedupe_key_is_version_free_and_carries_the_listing_id():
    base = reconciler.dedupe_key(900001, "street_not_in_obec", "street_name", "A", "B")
    assert base == reconciler.dedupe_key(900001, "street_not_in_obec", "street_name", "A", "B")
    assert base != reconciler.dedupe_key(900002, "street_not_in_obec", "street_name", "A", "B")
    assert base != reconciler.dedupe_key(900001, "street_not_in_obec", "obec_name", "A", "B")


def test_the_dedupe_key_normalizes_so_a_diacritic_is_not_a_new_card():
    assert reconciler.dedupe_key(1, "r", "street_name", "Nad Bořislavkou", "x") == (
        reconciler.dedupe_key(1, "r", "street_name", "nad borislavkou", "x")
    )


def test_a_reconciler_version_bump_cannot_change_the_key():
    """Bumping `reconciler_version` is routine — one per shipped rule — and every bump would
    otherwise orphan every operator judgement and re-flood the queue."""
    import inspect

    source = inspect.getsource(reconciler.dedupe_key)
    for excluded in ("reconciler_version", "registry_version", "snapshot_id"):
        assert excluded not in source


# ---------------------------------------------------------------------------- rules


def test_street_not_in_obec_fires_on_a_street_absent_from_the_resolved_obec():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Slunečná"),  # exists only in Bílovec
        mm.claim(3, "coordinate", lat=50.0755, lon=14.4378),
    ]
    _, detections = _run(claims)
    fired = {d.rule for d in detections}
    assert "street_not_in_obec" in fired
    assert next(d for d in detections if d.rule == "street_not_in_obec").severity == "major"


def test_the_remax_carousel_class_opens_a_contradiction_without_ever_winning():
    """2 144 of 4 918 street-bearing active remax rows (43.6 %) carry a `data-address`
    street from the 'Podobné nemovitosti' block. Stored, never rankable, always compared."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="remax"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou", source="remax"),
        mm.claim(3, "street_name", value_text="Krymská", source="remax",
                 subject_scoped=False, extraction_method="html_selector_parse"),
    ]
    resolution, detections = _run(claims)
    assert resolution.fields["street_name"].value == "Nad Bořislavkou"
    assert "street_from_excluded_block_vs_served" in {d.rule for d in detections}


def test_house_number_disagreement_is_major_and_names_both_claims():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "house_number_cp", value_text="487"),
        mm.claim(3, "house_number_cp", value_text="512", source="remax"),
    ]
    _, detections = _run(claims)
    detection = next(d for d in detections if d.rule == "house_number_disagreement")
    assert detection.severity == "major"
    assert set(detection.evidence_claim_ids) == {2, 3}


def test_the_post_town_mismatch_is_informational_never_an_error():
    """`"696 81 Hodonín"` → locality Hodonín while the geom-derived obec is Bzenec; the two
    disagree on 57.0 % of bazos rows and neither is wrong."""
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="bazos"),
        mm.claim(2, "postal_town", value_text="Vokovice", source="bazos"),
        mm.claim(3, "coordinate", lat=50.0755, lon=14.4378, source="bazos"),
    ]
    _, detections = _run(claims)
    detection = next(d for d in detections if d.rule == "postal_city_vs_obec")
    assert detection.severity == "info"


def test_the_pin_registry_distance_signal_reaches_the_ledger():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Nad Bořislavkou 487/40"),
        mm.claim(3, "coordinate", lat=50.2000, lon=14.4000),
    ]
    _, detections = _run(claims)
    detection = next(d for d in detections if d.rule == "pin_registry_distance")
    assert detection.distance_m > 300
    assert detection.auto_action == "downgraded_precision"


def test_the_country_dispute_reaches_the_ledger_as_major():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="remax"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378, source="remax"),
        mm.claim(3, "foreign_indicator", value_text="Polsko", source="remax",
                 extraction_method="llm_text"),
    ]
    _, detections = _run(claims)
    detection = next(d for d in detections if d.rule == "country_dispute")
    assert detection.severity == "major"


def test_obec_claim_vs_resolution_only_fires_where_the_portal_string_is_trusted():
    """The rule is PER-SOURCE enabled: `locality` is correct 12/12 on remax, and wrong on
    maxima (ambiguous Krásný Les) and realitymix (unaccented Bilovec)."""
    claims = [
        mm.claim(1, "obec_name", value_text="Bílovec", source="remax"),
        mm.claim(2, "coordinate", lat=50.0755, lon=14.4378, source="remax"),
    ]
    _, off = _run(claims)
    _, on = _run(claims, per_source_locality_trusted=True)
    assert "obec_claim_vs_resolution" not in {d.rule for d in off}
    assert "obec_claim_vs_resolution" in {d.rule for d in on}


def test_detections_are_ordered_most_severe_first_and_deterministically():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha", source="bazos"),
        mm.claim(2, "street_name", value_text="Slunečná", source="bazos"),
        mm.claim(3, "postal_town", value_text="Vokovice", source="bazos"),
        mm.claim(4, "coordinate", lat=50.0755, lon=14.4378, source="bazos"),
    ]
    _, first = _run(claims)
    _, second = _run(claims)
    assert [d.dedupe_key for d in first] == [d.dedupe_key for d in second]
    severities = [d.severity for d in first]
    assert severities == sorted(severities, key=reconciler.SEVERITIES.index)


# ----------------------------------------------------------------------- auto-close


def test_auto_close_needs_both_a_silent_predicate_and_changed_inputs():
    stale = ["deadbeef"]
    assert reconciler.auto_close(stale, [], inputs_changed=False) == []
    closes = reconciler.auto_close(stale, [], inputs_changed=True)
    assert [c.dedupe_key for c in closes] == stale
    assert closes[0].status == "resolved_upstream"
    assert closes[0].decided_by == "reconciler"


def test_a_still_firing_finding_is_never_auto_closed():
    claims = [
        mm.claim(1, "obec_name", value_text="Praha"),
        mm.claim(2, "street_name", value_text="Slunečná"),
        mm.claim(3, "coordinate", lat=50.0755, lon=14.4378),
    ]
    _, detections = _run(claims)
    keys = [d.dedupe_key for d in detections]
    assert reconciler.auto_close(keys, detections, inputs_changed=True) == []
