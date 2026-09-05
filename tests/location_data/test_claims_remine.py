"""W3 snapshot re-mining — 06 §6.2.2's coordinate-history asymmetry and the snapshot
anchor/observation-time plumbing this module adds to `claims_intake`'s shared writer.

The rule set, stated once (see `location_data/claims_remine.py`'s module docstring for the
full ground-truth argument):
  * ONLY sreality may ever produce a `claim_type='coordinate'` claim from a snapshot — the
    other eight sources' `_HASH_FIELDS` allowlist excludes lat/lon, so a snapshot's mere
    existence is not evidence a coordinate was checked, and six of the eight have no
    coordinate VALUE in `raw_json` at all (only mmreality/bezrealitky do, but even they are
    excluded per the hash fact).
  * Every claim this lane writes carries `snapshot_anchor='snapshot'` and a real
    `snapshot_id` — never the `unanchored_*` values `claims_intake` itself would stamp.
  * `history_completeness` is `'full'` for sreality and `'locality_text_only'` for the
    other eight, uniformly — not `claims_intake.HISTORY_COMPLETENESS`'s richer W1 mapping.
  * This lane never enrolls a row into the live refetch cohort (`location_enrichment_state`
    has no historical meaning) — `IntakeResult.enrichment` is always empty.
  * A value that changes across two snapshots produces two distinct `location_claims` rows
    (different `value_geom_wkt`/`value_text` -> different fingerprint); a value that repeats
    is the SAME claim, just re-stamped per call here (the re-sighting collapse itself is a
    SQL-level, not Python-level, guarantee — exercised by `test_claims_intake_fingerprint`'s
    sibling coverage of `_CLAIM_WRITE_SQL`, not re-derived here).
"""

from __future__ import annotations

import copy

import pytest

from location_data.claims_intake import EMITTABLE_LICENCE_CLASSES
from location_data.claims_remine import (
    COORDINATE_HISTORY_SOURCES,
    SOURCES,
    W3_HISTORY_COMPLETENESS,
    _entries_for_remine,
    _payload_lat_lon,
    remine_snapshot,
)
from tests.location_data.claim_intake_fixtures import (
    BAZOS_LINK,
    BEZREALITKY,
    IDNES_PAGE,
    MMREALITY_ACCURATE,
    SREALITY_LEGACY,
    SREALITY_POST_CUTOVER,
    SREALITY_TRUNCATED,
    claims_by_type,
    entries_for,
    listing,
)


def snapshot_row(source: str, raw_json: dict, **overrides):
    """The SAME construction `location_data.claims_remine.run()` performs per snapshot
    row: no `listings.geom`-derived lat/lon (there is none historically), no legacy
    columns (there are none historically) — only what `_payload_lat_lon` can peek out of
    the payload itself, via whichever contract entry declares `lat_pointer`/`lon_pointer`."""
    entries = entries_for(source)
    lat, lon = _payload_lat_lon(raw_json, entries)
    kwargs = {"lat": lat, "lon": lon, "locality": None, "street": None,
              "street_source": None}
    kwargs.update(overrides)
    return listing(source, raw_json, **kwargs), entries


# ------------------------------------------------------------ coordinate-history scoping

def test_only_sreality_is_coordinate_eligible():
    assert COORDINATE_HISTORY_SOURCES == frozenset({"sreality"})


@pytest.mark.parametrize("source", sorted(set(SOURCES) - COORDINATE_HISTORY_SOURCES))
def test_coordinate_entries_are_filtered_out_for_every_other_source(source):
    entries = entries_for(source)
    if not any(e.claim_type == "coordinate" for e in entries):
        pytest.skip(f"{source}'s contract declares no coordinate entry at all")
    scoped = _entries_for_remine(entries, source)
    assert not any(e.claim_type == "coordinate" for e in scoped)


def test_sreality_keeps_its_coordinate_entry():
    entries = entries_for("sreality")
    scoped = _entries_for_remine(entries, "sreality")
    assert any(e.claim_type == "coordinate" for e in scoped)
    assert len(scoped) == len(entries)


def test_mmreality_snapshot_never_produces_a_coordinate_claim_despite_a_payload_point():
    """MMREALITY_ACCURATE carries a first-party `point{}`. W3 must not claim it, because a
    snapshot's existence is not evidence mmreality's coordinate was checked (its lat/lon sit
    outside `_HASH_FIELDS`). Two independent rails now say so — `_entries_for_remine` drops
    every coordinate entry off the non-sreality lanes, and from mmreality@2 the entry names
    an ARCHIVE reader W1's loop skips anyway.

    The PEEK must survive both, and it is not a claim: `_payload_lat_lon` feeds
    `extract_listing`'s withheld-coordinate absence heuristic, which a `SnapshotRow` has no
    `listings.geom` to feed otherwise. Keying it on the reader NAME lost it the moment
    mm.det.point stopped being `point_pair`, silently — hence the locator-key assertion."""
    assert _payload_lat_lon(MMREALITY_ACCURATE, entries_for("mmreality")) != (None, None)
    row, entries = snapshot_row("mmreality", MMREALITY_ACCURATE)
    result = remine_snapshot(101, row, entries)
    assert "coordinate" not in claims_by_type(result)
    # The non-coordinate structured signal the payload DOES support is still mined —
    # scoping removes ONE claim type, not the whole portal.
    assert claims_by_type(result), "mmreality should still yield non-coordinate claims"


def test_sreality_snapshot_does_produce_a_coordinate_claim():
    row, entries = snapshot_row("sreality", SREALITY_POST_CUTOVER)
    result = remine_snapshot(101, row, entries)
    assert "coordinate" in claims_by_type(result)


@pytest.mark.parametrize("source,payload", [
    ("bazos", BAZOS_LINK),
    ("idnes", IDNES_PAGE),
])
def test_geom_column_substrate_sources_have_no_coordinate_to_peek_at_all(source, payload):
    """bazos/idnes (and the other geom_column-substrate portals) never carried the
    coordinate VALUE in raw_json to begin with (06 §6.1.3) — `_payload_lat_lon` must find
    nothing to peek at, independent of the entry-filtering rule above."""
    entries = entries_for(source)
    lat, lon = _payload_lat_lon(payload, entries)
    assert (lat, lon) == (None, None)


# ------------------------------------------------------------------- snapshot anchoring

def test_every_claim_is_snapshot_anchored_with_the_right_id():
    row, entries = snapshot_row("sreality", SREALITY_POST_CUTOVER)
    result = remine_snapshot(4242, row, entries)
    assert result.claims, "fixture should yield at least one claim"
    for claim in result.claims:
        assert claim.snapshot_anchor == "snapshot"
        assert claim.snapshot_id == 4242


def test_absences_are_also_snapshot_anchored():
    """A licence-refused coordinate is a snapshot-scoped negative assertion too."""
    row, entries = snapshot_row("sreality", SREALITY_POST_CUTOVER, in_mapy_inventory=True)
    result = remine_snapshot(777, row, entries)
    assert "coordinate" not in claims_by_type(result)
    coordinate_absences = [a for a in result.absences if a.field_ == "coordinate"]
    assert coordinate_absences
    assert all(a.snapshot_id == 777 for a in coordinate_absences)


def test_history_completeness_matches_the_w3_gate_mapping():
    for source in SOURCES:
        expected = "full" if source in COORDINATE_HISTORY_SOURCES else "locality_text_only"
        assert W3_HISTORY_COMPLETENESS[source] == expected


@pytest.mark.parametrize("source,payload", [
    ("bazos", BAZOS_LINK),
    ("bezrealitky", BEZREALITKY),
])
def test_non_sreality_claims_are_stamped_locality_text_only(source, payload):
    row, entries = snapshot_row(source, payload)
    result = remine_snapshot(1, row, entries)
    assert result.claims, f"{source} fixture should yield at least one claim"
    for claim in result.claims:
        assert claim.history_completeness == "locality_text_only"


def test_sreality_claims_are_stamped_full():
    row, entries = snapshot_row("sreality", SREALITY_POST_CUTOVER)
    result = remine_snapshot(1, row, entries)
    assert result.claims
    for claim in result.claims:
        assert claim.history_completeness == "full"


# --------------------------------------------------------------- never a live refetch

@pytest.mark.parametrize("payload", [SREALITY_LEGACY, SREALITY_TRUNCATED])
def test_legacy_shape_snapshots_never_enroll_in_the_refetch_cohort(payload):
    """A legacy-shape or truncated snapshot is an accurate historical fact, not a gap a
    live refetch could ever close — `location_enrichment_state` stays untouched."""
    row, entries = snapshot_row("sreality", payload)
    result = remine_snapshot(1, row, entries)
    assert result.enrichment == []


def test_truncated_snapshot_still_records_the_absence():
    """The refetch-cohort suppression must not also suppress the negative ASSERTION (03
    §3.2 rule 4: every attempt is recorded, including negatives) — only the future-work
    routing is meaningless for a snapshot, not the fact itself."""
    row, entries = snapshot_row("sreality", SREALITY_TRUNCATED)
    result = remine_snapshot(1, row, entries)
    assert any(a.field_ == "coordinate" and "truncat" in (a.detail or "")
               for a in result.absences)


def test_legacy_shape_snapshot_also_records_the_absence():
    """W1's own `extract_listing()` records NO absence at all for `shape == 'legacy'`
    (only 'absent'/truncated) — fine for W1, where the refetch-cohort enrollment already
    surfaces the row. W3 disables that enrollment (the tests above), so without this the
    legacy-shape cohort would be a SILENT hole, indistinguishable from "not yet re-mined".
    `remine_snapshot` must close it: `_read_point_pair` never yields a coordinate claim for
    a legacy-shape payload (no `gps_lat`/`gps_lon` at the post-cutover locator), and this
    asserts the compensating absence exists instead."""
    row, entries = snapshot_row("sreality", SREALITY_LEGACY)
    result = remine_snapshot(1, row, entries)
    assert "coordinate" not in claims_by_type(result)
    coordinate_absences = [a for a in result.absences if a.field_ == "coordinate"]
    assert coordinate_absences, "a legacy-shape snapshot must not be a silent hole"
    assert all(a.snapshot_id == 1 for a in coordinate_absences)


def test_enrichment_is_always_empty_even_with_an_oversized_value():
    """The oversized-value guard inside `extract_listing()` is NOT flag-gated (it protects
    W1 too), so this asserts the second rail: `remine_snapshot` drops `result.enrichment`
    unconditionally, regardless of why `extract_listing()` populated it."""
    oversized = copy.deepcopy(SREALITY_POST_CUTOVER)
    oversized["locality"]["geometry"]["geometry"] = ["x" * (3 * 1024 * 1024)]
    row, entries = snapshot_row("sreality", oversized)
    result = remine_snapshot(1, row, entries)
    assert result.enrichment == []


# ------------------------------------------------------------------------- oscillation

def test_a_changed_coordinate_across_two_snapshots_yields_two_distinct_claim_values():
    """The corpus-level guarantee ('claim_fingerprint is TIME-FREE, so two DIFFERENT
    values anchor two DIFFERENT rows') is a SQL/DB-level fact this pure-Python test cannot
    exercise directly — but the precondition for it is that two genuinely different
    observations produce two Claim objects with different value payloads, which is what
    this asserts. A per-listing precision/coordinate time series (06 §6.4's W3 gate) is
    exactly this sequence of distinct values, ordered by `first_observed_at`."""
    early = copy.deepcopy(SREALITY_POST_CUTOVER)
    later = copy.deepcopy(SREALITY_POST_CUTOVER)
    later["locality"]["gps_lat"] = 50.09  # a genuine, later re-pin
    later["locality"]["gps_lon"] = 14.47

    row_early, entries = snapshot_row("sreality", early)
    row_later, _ = snapshot_row("sreality", later)

    claim_early = claims_by_type(remine_snapshot(10, row_early, entries))["coordinate"][0]
    claim_later = claims_by_type(remine_snapshot(20, row_later, entries))["coordinate"][0]

    assert claim_early.value_geom_wkt != claim_later.value_geom_wkt
    assert claim_early.snapshot_id == 10
    assert claim_later.snapshot_id == 20


def test_a_repeated_value_across_two_snapshots_yields_the_same_claim_value():
    row1, entries = snapshot_row("sreality", SREALITY_POST_CUTOVER)
    row2, _ = snapshot_row("sreality", SREALITY_POST_CUTOVER)

    claim1 = claims_by_type(remine_snapshot(10, row1, entries))["coordinate"][0]
    claim2 = claims_by_type(remine_snapshot(20, row2, entries))["coordinate"][0]

    # Identical value -> identical fingerprint inputs (everything the SQL tuple hashes
    # over except snapshot_id/first_observed_at, which are deliberately outside it).
    assert claim1.value_geom_wkt == claim2.value_geom_wkt
    assert claim1.extractor_id == claim2.extractor_id
    assert claim1.extractor_version == claim2.extractor_version


# --------------------------------------------------------------------- licence class

def test_every_claim_stays_within_the_emittable_licence_classes():
    for source in SOURCES:
        payload = {
            "sreality": SREALITY_POST_CUTOVER, "bazos": BAZOS_LINK,
            "bezrealitky": BEZREALITKY, "mmreality": MMREALITY_ACCURATE,
            "idnes": IDNES_PAGE,
        }.get(source)
        if payload is None:
            continue
        row, entries = snapshot_row(source, payload)
        result = remine_snapshot(1, row, entries)
        for claim in result.claims:
            assert claim.licence_class in EMITTABLE_LICENCE_CLASSES
