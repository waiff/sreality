"""mmreality@2 — the SHIPPED contract, run over the REAL archived page.

`test_archive_reader_canon` proves the readers; this proves the CONTRACT that names them.
Every entry mmreality@2 activates is executed here through `extract_payload`, over
`tests/fixtures/portal_html/mmreality_detail.html` (listing 951845, a genuinely archived
body carrying three `:property` blobs) and over the pinned
`tests/fixtures/location_w2/mmreality_detail.html` the fixture-diff gate scores.

The one property that makes this portal's activation worth a version bump: the NEIGHBOUR's
blob on the archived body is the LARGER one (23,656 source characters against the subject's
13,827), so `scraper.mmreality_parser`'s "largest blob by serialized length" fallback — which
`listings.raw_json` inherits and cannot report — returns Ludvíkov's street, obec and pin for
an Andělská Hora listing. Nothing below may carry that listing's values, and the non-vacuity
assertion is what stops a green empty run from reading as a pass.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from location_data import contracts
from location_data.claims_intake import Entry, IntakeRefused, ListingRow
from location_data.claims_remine_archive import (
    ARCHIVE_READERS,
    ArchivedPayload,
    _DUMMY_LEGACY_COLUMNS,
    extract_payload,
)
from location_data.html_scope import ScopeRegister, scope_html

_ROOT = Path(__file__).resolve().parents[2]
_ARCHIVED_BODY = _ROOT / "tests" / "fixtures" / "portal_html" / "mmreality_detail.html"
_PINNED_BODY = _ROOT / "tests" / "fixtures" / "location_w2" / "mmreality_detail.html"

FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)

# The subject of the archived body, and the neighbour whose blob is the larger one. A test
# that scored these under a synthetic native id would go green EMPTY, which is the single
# failure mode an id-matched contract has.
SUBJECT = "951845"
NEIGHBOUR = "950647"

# What the neighbour's blob says. Not one of these may appear in a claim mined for 951845.
NEIGHBOUR_VALUES = ("Ludvíkov", "950647", "17.347457655", "50.113874456")

CONTRACT = {c.source: c for c in contracts.load_all()}["mmreality"]
ARCHIVE_ENTRIES = [e for e in CONTRACT.entries if e.reader in ARCHIVE_READERS]


def entries() -> list[Entry]:
    """The shipped entries in the runtime's own shape — `contracts.parse_entry` output
    projected exactly as `load_entries` projects it out of the DB."""
    return [
        Entry(
            id=8000 + index, source=CONTRACT.source, contract_id=1,
            contract_version=CONTRACT.version, entry_id=e.entry_id, surface=e.surface,
            page_kind=e.page_kind, locator=e.locator, claim_type=e.claim_type,
            extraction_method=e.extraction_method, subject_scope=e.subject_scope,
            transform=tuple(e.transform), precision_map=e.precision_map,
            default_blur_evidence=e.default_blur_evidence,
            default_licence_class=e.default_licence_class, cardinality=e.cardinality,
            guards=tuple(e.guards))
        for index, e in enumerate(ARCHIVE_ENTRIES)
    ]


def row(native: str, *, in_mapy_inventory: bool = False) -> ListingRow:
    return ListingRow(
        listing_id=951845, source="mmreality", source_id_native=native, raw_json={},
        lat=None, lon=None, observed_at=FETCHED_AT,
        in_mapy_inventory=in_mapy_inventory,
        legacy_columns=dict(_DUMMY_LEGACY_COLUMNS))


def payload(native: str, body: bytes) -> ArchivedPayload:
    return ArchivedPayload(
        id=9001, source="mmreality", source_id_native=native, page_kind="detail",
        payload_sha256="ab" * 32, first_observed_at=FETCHED_AT, body=body)


def register() -> ScopeRegister:
    return ScopeRegister.from_zones("mmreality", CONTRACT.exclusion_zones)


def run(native: str, path: Path = _ARCHIVED_BODY, *, items: list[Entry] | None = None,
        in_mapy_inventory: bool = False):
    body = path.read_bytes()
    return extract_payload(
        payload(native, body), row(native, in_mapy_inventory=in_mapy_inventory),
        items if items is not None else entries(), register=register())


def by_id(result) -> dict[str, object]:
    found = {}
    for claim in result.claims:
        assert claim.extractor_id not in found, f"{claim.extractor_id} twice"
        found[claim.extractor_id] = claim
    return found


# ------------------------------------------------- the contract, as shipped

def test_mmreality_ships_at_version_two_in_shadow():
    """Shadow is HEADER-grain and its cost is stated in the YAML: it darkens the portal's
    already-live W1 claims too, until `--unshadow mmreality@2`. It ships anyway because
    W2-13 gives `claims_remine_archive` a dispatcher, and
    `test_a_dom_contract_must_be_shadowed_once_a_lane_can_run_it` is the rail that says a
    runnable DOM contract may not be live."""
    assert CONTRACT.version == 2
    assert CONTRACT.shadow is True


def test_the_activated_entries_are_exactly_the_seven_this_wave_names():
    """Enumerated, not derived: an entry that quietly gained or lost a reader is the
    difference between a portal being mined and a portal being silent, and neither direction
    announces itself at runtime."""
    assert {e.entry_id for e in ARCHIVE_ENTRIES} == {
        "mm.det.point", "mm.det.original_title_street", "mm.det.blob_accurate",
        "mm.det.blob_street", "mm.det.blob_municipality",
        "mm.det.blob_municipality_part", "mm.det.blob_municipality_id",
    }
    for entry in ARCHIVE_ENTRIES:
        assert entry.subject_scope.get("kind") == "id_match", entry.entry_id
        assert entry.subject_scope.get("on_miss") == "fail", entry.entry_id
        assert entry.locator["match"] == {
            "json_pointer": "/id", "equals_row_field": "source_id_native"}, entry.entry_id


def test_the_w1_entries_the_twins_shadow_keep_their_own_readers():
    """The five new ids are TWINS, not replacements: `claim_fingerprint` hashes `surface`, so
    the raw_json row and the archived row are two rows and both survive into
    `location_claims_live`. Restating the W1 entries onto archive readers instead would take
    mmreality's hourly admin claims dark for the sake of a corroboration."""
    readers = {e.entry_id: e.reader for e in CONTRACT.entries}
    assert readers["mm.det.accurate"] == "declared_bool_quality"
    assert readers["mm.det.municipality"] == "scalar"
    assert readers["mm.det.municipality_id"] == "scalar"
    assert readers["mm.det.municipality_part"] == "scalar"
    assert readers["mm.det.street"] == "scalar"


# --------------------------------------------- the archived body, listing 951845

def test_the_subject_blob_is_read_and_the_larger_neighbour_blob_is_not():
    result = run(SUBJECT)
    found = by_id(result)

    assert set(found) == {
        "mm.det.point", "mm.det.blob_accurate", "mm.det.blob_municipality",
        "mm.det.blob_municipality_part", "mm.det.blob_municipality_id",
    }, "non-vacuity: a re-capture that breaks id matching must fail HERE, not go green empty"

    point = found["mm.det.point"]
    assert point.value_geom_wkt == "POINT(17.389086312 50.060813844)"
    assert point.value_text == "50.060813844,17.389086312", "9 dp stored verbatim, never re-rounded"
    assert point.licence_class == "portal"
    assert point.blur_evidence == "none"
    assert point.evidence_quote.startswith('"point":{')

    assert found["mm.det.blob_municipality_id"].value_text == "551929"
    assert found["mm.det.blob_municipality_id"].value_num == 551929.0
    assert found["mm.det.blob_municipality_id"].claim_type == "obec_code"

    assert found["mm.det.blob_municipality"].value_text == "Andělská Hora"
    assert found["mm.det.blob_municipality"].claim_type == "obec_name"
    assert found["mm.det.blob_municipality_part"].value_text == "Andělská Hora"
    assert found["mm.det.blob_municipality_part"].claim_type == "cast_obce_name"

    accurate = found["mm.det.blob_accurate"]
    assert accurate.value_text == "accurate"
    assert accurate.declared_precision_label == "accurate"
    assert accurate.value_num == 1.0
    assert accurate.blur_evidence == "none", "only `not_accurate` is in blurred_labels"

    body = " ".join(
        f"{c.value_text} {c.value_geom_wkt} {c.evidence_quote}" for c in result.claims)
    for decoy in NEIGHBOUR_VALUES:
        assert decoy not in body, decoy


def test_the_two_street_signals_are_honest_absences_on_this_listing():
    """951845 publishes no `/street` key and its `originalTitle` carries no `ul.` — the real
    `when_present` / `best_effort` misses this body was pinned for. A reader that invented a
    value here would be reading the neighbour's blob, which DOES carry a street."""
    found = by_id(run(SUBJECT))
    assert "mm.det.blob_street" not in found
    assert "mm.det.original_title_street" not in found
    # A miss inside the subject's own blob is not a subject miss: nothing to record.
    assert run(SUBJECT).absences == []


def test_every_archived_claim_carries_a_span_into_the_entity_encoded_attribute():
    """The evidence set migration 382 requires, plus the mmreality-specific half of it:
    the portal JSON-escapes accents and the serialiser entity-encodes the attribute's
    quotes, so a claim quoting its DECODED value would have no span at all. Every quote here
    is the JSON MEMBER SOURCE SLICE, and the span it resolves to is inside the
    `&quot;`-encoded attribute."""
    body = _ARCHIVED_BODY.read_bytes()
    document = scope_html(body, register=register())
    result = run(SUBJECT)
    assert result.claims
    for claim in result.claims:
        assert claim.payload_scope_version == document.scope_version, claim.extractor_id
        assert claim.evidence_quote, claim.extractor_id
        assert claim.span_start is not None and claim.span_end is not None, claim.extractor_id
        span = document.html[claim.span_start:claim.span_end]
        assert "&quot;" in span, (claim.extractor_id, span[:60])
        assert claim.surface == "archived_html"
        assert claim.page_kind == "detail"
        assert claim.subject_scoped is True


def test_the_selector_is_id_driven_and_not_position_driven():
    """Scored under the NEIGHBOUR's id, the same page yields the neighbour's location — which
    is the proof that the subject is chosen by `/id` and not by document order or size."""
    found = by_id(run(NEIGHBOUR))
    assert found["mm.det.blob_municipality"].value_text == "Ludvíkov"
    assert found["mm.det.point"].value_geom_wkt == "POINT(17.347457655 50.113874456)"
    assert "Andělská Hora" not in {
        c.value_text for c in run(NEIGHBOUR).claims}


# ------------------------------------------------------------ the refusals

def test_a_subject_miss_is_one_absence_per_entry_and_never_a_silent_zero():
    """`on_miss: fail`. Without the absences, "the portal changed its id scheme fleet-wide"
    and "this page genuinely carried no address" would be the same green zero-claim sweep,
    and the batch would still stamp 'ok' and move the watermark."""
    result = run("999999")
    assert result.claims == []
    assert len(result.absences) == len(ARCHIVE_ENTRIES)
    assert {a.reason for a in result.absences} == {"not_attempted"}
    assert {a.surface for a in result.absences} == {"archived_html"}
    assert {a.field_ for a in result.absences} == {
        "coordinate", "street_name", "precision_declaration", "obec_name",
        "cast_obce_name", "obec_code"}
    assert all("on_miss=fail" in str(a.detail) for a in result.absences)


def test_an_on_miss_other_than_fail_is_refused_rather_than_quietly_different():
    """The largest-blob fallback must not be reachable by re-declaring it. It is contract
    data, so the refusal fires on the first row of the portal or never."""
    items = [
        replace(e, subject_scope={**e.subject_scope, "on_miss": "largest_blob"})
        for e in entries()
    ]
    with pytest.raises(IntakeRefused) as raised:
        run(SUBJECT, items=items)
    assert "on_miss" in str(raised.value)


def test_the_mapy_inventory_veto_still_reaches_the_archived_coordinate():
    """C6 is decided once, in `ARCHIVED_COORDINATE_RULES`, and the §6.4 inventory gate is a
    JOIN on listing_id — not on the coordinate's substrate. Moving the entry onto the
    archived lane must not have moved it out from under the veto."""
    result = run(SUBJECT, in_mapy_inventory=True)
    assert "mm.det.point" not in by_id(result)
    refused = [a for a in result.absences if a.field_ == "coordinate"]
    assert len(refused) == 1 and refused[0].reason == "not_attempted"
    # The admin twins are unaffected: the veto is about a POSITION's provenance.
    assert "mm.det.blob_municipality_id" in by_id(result)


def test_the_coordinate_entry_keeps_the_id_the_licence_ladder_names():
    """`ARCHIVED_COORDINATE_RULES['mmreality']` names ONE entry as the portal's only
    licensable archived coordinate locator. Renaming the entry would not fail — it would
    silently unlicense every mmreality pin, which is why the id survived the bump."""
    from location_data.claims_intake import ARCHIVED_COORDINATE_RULES

    rule = ARCHIVED_COORDINATE_RULES["mmreality"]
    assert rule.entry_id == "mm.det.point"
    assert rule.licence_class == "portal"
    assert rule.geocoded_licence_class is None, (
        "no geocoded branch, so `position_branch: portal_geocoded` would be refused "
        "coordinate_provenance_unestablished on every row")
    entry = {e.entry_id: e for e in ARCHIVE_ENTRIES}["mm.det.point"]
    assert entry.locator["position_branch"] == "portal_pin"


# ------------------------------------------- the pinned body the golden gate scores

def test_the_pinned_fixture_exercises_the_two_entries_the_archived_body_cannot():
    """The archived body has no `/street` and no `ul.` title, so the pinned fixture is where
    `mm.det.blob_street` and `mm.det.original_title_street` are actually executed. Its
    subject blob is keyed `"fixture"` because that is the native id `score_archived` builds
    its row with; its accented values are `\\uXXXX`-escaped and its attribute is
    entity-encoded, exactly as production serves them."""
    found = by_id(run("fixture", _PINNED_BODY))
    assert set(found) == {
        "mm.det.point", "mm.det.original_title_street", "mm.det.blob_accurate",
        "mm.det.blob_street", "mm.det.blob_municipality",
        "mm.det.blob_municipality_part", "mm.det.blob_municipality_id",
    }
    assert found["mm.det.blob_street"].value_text == "Křižíkova"
    assert found["mm.det.blob_street"].evidence_quote.startswith('"street":')
    assert found["mm.det.original_title_street"].value_text == "Křižíkova"
    assert found["mm.det.original_title_street"].evidence_quote.startswith(
        '"originalTitle":'), (
        "the member slice, not the capture: the escaped street also occurs in `title`, "
        "`location` and `slug`, and a span on the wrong occurrence still satisfies "
        "migration 382's substring CHECK")
    assert found["mm.det.point"].value_geom_wkt == "POINT(14.45118 50.09239)"
    assert found["mm.det.blob_municipality"].value_text == "Praha"


def test_the_pinned_fixture_keeps_the_larger_neighbour_blob_that_makes_it_a_test():
    """`scope: non_subject_blobs` is unhonourable by a DOM strip, so BOTH blobs survive
    scoping — subject selection is the reader's job. The decoy is deliberately the longer
    one; a fixture where it is not would stop testing the removed fallback."""
    document = scope_html(_PINNED_BODY.read_bytes(), register=register())
    blobs = document.css("[\\:property]")
    assert len(blobs) == 2
    subject, decoy = (b.attributes[":property"] for b in blobs)
    assert len(decoy) > len(subject)
    assert '"id":"fixture"' in subject and '"id":950647' in decoy
    body = " ".join(
        f"{c.value_text} {c.evidence_quote}" for c in run("fixture", _PINNED_BODY).claims)
    for decoy_value in ("Sokolovská", "Kladno", "950647"):
        assert decoy_value not in body, decoy_value
