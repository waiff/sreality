"""W2 archived-HTML re-mining — the evidence discipline, the archived licence ladder, and
the batch semantics the lane inherits from W1.

The lane is INERT on merge (no portal declares an archived reader yet), so every test here
either drives a synthetic contract entry through a fake reader, or asserts a property of
the plumbing itself. Two families carry real weight:

  * EVIDENCE. Migration 382's `loc_claim_text_evidence` / `loc_claim_evidence_payload` are
    the LAST line of defence. A batch is one transaction, so a single malformed claim rolls
    back every good claim beside it — and the DB's error names a constraint, not the entry.
    `assert_evidence_complete` must refuse first, and `test_the_python_validator_requires_
    exactly_what_the_db_check_requires` reads the applied DDL so the two cannot drift.
  * THE LADDER. `mapy_affected` membership vetoes a coordinate on the archived substrate
    exactly as it does on `raw_json` (06 §6.4's gate joins on `listing_id`, not on
    `surface`), and C6's licence spellings are pinned against the enum.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from location_data import claims_intake, claims_remine, claims_remine_archive
from location_data.claims_intake import (
    ARCHIVED_COORDINATE_RULES,
    DEFAULT_WRITE_CHUNK_BYTES,
    DEFAULT_WRITE_CHUNK_ROWS,
    SUBSTRATE_ARCHIVED_HTML,
    Claim,
    Entry,
    IntakeRefused,
    ListingRow,
    _base,
    chunk_rows,
    coordinate_verdict,
    dedupe_absence_rows,
)
from location_data.claims_remine_archive import (
    ARCHIVE_ANCHOR,
    ARCHIVE_HISTORY_COMPLETENESS,
    ARCHIVE_READERS,
    ARCHIVE_SURFACE,
    ArchivedPayload,
    archive_entries,
    assert_evidence_complete,
    assert_stampable,
    extract_payload,
    run,
    stamp_archive_claim,
)
from location_data.html_scope import ScopeRegister

_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_382 = (_ROOT / "migrations" / "382_location_w1_claims.sql").read_text("utf-8")

OBSERVED_AT = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 13, 4, 30, tzinfo=UTC)
BODY = b"<html><body><div id='subject'>Krymska 12, Praha 10</div></body></html>"
EMPTY_REGISTER = ScopeRegister.from_zones("remax", ())


def archive_entry(
    entry_id: str = "rx.det.street",
    *,
    source: str = "remax",
    claim_type: str = "street_name",
    extraction_method: str = "html_selector_parse",
    surface: str = "html_selector",
    page_kind: str = "detail",
    reader: str = "fake_html",
    licence_class: str = "portal",
    blur_evidence: str = "none",
) -> Entry:
    return Entry(
        id=7001, source=source, contract_id=1, contract_version=2, entry_id=entry_id,
        surface=surface, page_kind=page_kind, locator={"reader": reader, "css": "#subject"},
        claim_type=claim_type, extraction_method=extraction_method, subject_scope={},
        transform=(), precision_map={}, default_blur_evidence=blur_evidence,
        default_licence_class=licence_class, cardinality="one", guards=())


def listing_row(**overrides: Any) -> ListingRow:
    kwargs: dict[str, Any] = {
        "listing_id": 4242, "source": "remax", "source_id_native": "445781",
        "raw_json": {}, "lat": None, "lon": None, "observed_at": FETCHED_AT,
        "in_mapy_inventory": False,
        "legacy_columns": dict(claims_remine_archive._DUMMY_LEGACY_COLUMNS),
    }
    kwargs.update(overrides)
    return ListingRow(**kwargs)


def payload(**overrides: Any) -> ArchivedPayload:
    kwargs: dict[str, Any] = {
        "id": 9001, "source": "remax", "source_id_native": "445781", "page_kind": "detail",
        "payload_sha256": "ab" * 32, "first_observed_at": FETCHED_AT, "body": BODY,
    }
    kwargs.update(overrides)
    return ArchivedPayload(**kwargs)


def raw_claim(entry: Entry | None = None, **overrides: Any) -> Claim:
    """What a reader hands back: `_base`'s stamping and nothing else. The archived
    provenance is `stamp_archive_claim`'s job, which is exactly what the tests below
    exercise."""
    entry = entry or archive_entry()
    return _base(entry, listing_row(), value_text="Krymská", **overrides)


# ------------------------------------------------------- the disambiguated lane identity

def test_this_lane_never_shares_an_identifier_with_the_snapshot_re_mine_lane():
    """`location_claim_batches` resume/watermark is keyed on (lane, source, scan_mode). W3
    re-mines `listing_snapshots`, this one re-mines archived bodies, and both design
    documents independently named their module `claims_remine` with
    LANE='location_claims_remine'. One shared lane string is not a crash — it is two
    cursors silently overwriting each other's coverage."""
    assert claims_remine_archive.LANE != claims_remine.LANE
    assert claims_remine_archive.REMINE_VERSION != claims_remine.REMINE_VERSION
    assert claims_remine_archive.JOB_NAME != claims_remine.JOB_NAME
    assert claims_remine_archive.CONCURRENCY_GROUP != claims_remine.CONCURRENCY_GROUP
    assert claims_remine_archive.LANE == "location_claims_remine_archive"
    assert claims_remine_archive.REMINE_VERSION == "claims_remine_archive@1"
    assert claims_remine_archive.WAVE == "W2"


def test_the_archive_reader_registry_is_separate_from_w1s():
    """`mm.det.point` declares `reader: point_pair`, a W1 PAYLOAD reader. Sharing one
    registry would make this lane re-run W1's raw_json reads under `surface='archived_html'`
    on this lane's batch id."""
    assert ARCHIVE_READERS is not claims_intake.READERS
    assert not set(ARCHIVE_READERS) & set(claims_intake.READERS)


def test_no_portal_declares_an_archived_reader_yet_so_the_lane_is_inert():
    """The W2-2 acceptance condition, asserted rather than assumed. When a per-portal PR
    lands a reader this test is the one that must be updated deliberately."""
    assert ARCHIVE_READERS == {}


# ------------------------------------------------------------------ evidence discipline

def _evidence_columns_in_check(constraint: str) -> set[str]:
    body = re.search(
        rf"constraint\s+{constraint}\s+check\s*\((.*?)\)\);?",
        _MIGRATION_382, re.S | re.I)
    assert body, f"{constraint} not found in migration 382"
    return set(re.findall(r"\b(\w+) is not null", body.group(1), re.I))


def test_the_python_validator_requires_exactly_what_the_db_check_requires():
    """Read the APPLIED DDL, not the design prose. If a later migration widens
    `loc_claim_text_evidence` this fails here rather than at 3 a.m. inside a batch."""
    required = _evidence_columns_in_check("loc_claim_text_evidence")
    assert required == {"evidence_quote", "span_start", "span_end",
                        "payload_scope_version", "subject_scoped"}

    complete = raw_claim(
        archive_entry(extraction_method="regex_text"),
        evidence_quote="Krymská", span_start=10, span_end=17,
        payload_scope_version="html_scope@1:remax:deadbeefdeadbeef",
        payload_sha256="ab" * 32, subject_scoped=True)
    assert_evidence_complete(complete)

    for column in sorted(required):
        with pytest.raises(IntakeRefused) as excinfo:
            assert_evidence_complete(replace(complete, **{column: None}))
        assert column in str(excinfo.value)


def test_a_regex_text_claim_with_no_evidence_at_all_is_refused_before_the_write():
    """The headline: the CHECK is never the first line of defence."""
    with pytest.raises(IntakeRefused) as excinfo:
        assert_evidence_complete(raw_claim(archive_entry(extraction_method="regex_text")))
    message = str(excinfo.value)
    assert "evidence_quote" in message and "span_start" in message
    assert "payload_scope_version" in message
    assert "rx.det.street" in message, "the refusal must name the extractor, not a constraint"


def test_an_llm_text_claim_is_held_to_the_same_rule():
    with pytest.raises(IntakeRefused):
        assert_evidence_complete(raw_claim(archive_entry(extraction_method="llm_text")))


def test_a_degenerate_span_is_refused():
    for start, end in ((17, 17), (17, 10)):
        with pytest.raises(IntakeRefused, match="span_end"):
            assert_evidence_complete(raw_claim(
                archive_entry(extraction_method="regex_text"),
                evidence_quote="Krymská", span_start=start, span_end=end,
                payload_scope_version="v", payload_sha256="ab" * 32, subject_scoped=True))


def test_a_quote_without_a_payload_hash_is_refused_on_any_method():
    """`loc_claim_evidence_payload` is not scoped to the text methods: a span is meaningless
    without the document it indexes into, whoever produced it."""
    with pytest.raises(IntakeRefused, match="payload_sha256"):
        assert_evidence_complete(raw_claim(evidence_quote="Krymská", span_start=1, span_end=8))


def test_a_structured_claim_needs_no_span():
    """`loc_claim_text_evidence` binds `llm_text` / `regex_text` only — an
    `html_selector_parse` read of an attribute has no sentence to quote."""
    assert_evidence_complete(raw_claim())


def test_blur_evidence_is_clamped_to_the_two_values_a_migration_may_write():
    """06 §6.6 rule 7: 'detected'/'both' are the collision detector's, and the column
    DEFAULT stamping 'none' onto a row that carries a portal blur flag is unrecoverable in
    an append-only table."""
    for value in ("none", "declared"):
        assert_stampable(raw_claim(archive_entry(blur_evidence=value)))
    for value in ("detected", "both"):
        with pytest.raises(IntakeRefused, match="blur_evidence"):
            assert_stampable(raw_claim(archive_entry(blur_evidence=value)))


def test_only_portal_and_odbl_may_be_emitted():
    assert claims_remine_archive.ARCHIVE_EMITTABLE_LICENCE_CLASSES == {"portal", "odbl"}
    for value in ("ephemeral_display_only", "cc_by_ruian", "operator"):
        with pytest.raises(IntakeRefused, match="licence_class"):
            assert_stampable(raw_claim(archive_entry(licence_class=value)))


# ---------------------------------------------------------------- archived stamping (C9/C10/C4)

def test_every_claim_is_stamped_archived_html_with_the_pages_own_page_kind():
    stamped = stamp_archive_claim(raw_claim(), payload(page_kind="index"),
                                  scope_version="html_scope@1:remax:beef")
    assert stamped.surface == ARCHIVE_SURFACE == "archived_html"
    assert stamped.page_kind == "index", "C10: a body does not change what kind of page it is"
    assert stamped.snapshot_id is None
    assert stamped.snapshot_anchor == ARCHIVE_ANCHOR == "unanchored_latest_fetch"
    assert stamped.first_observed_at == FETCHED_AT
    assert stamped.payload_id == 9001
    assert stamped.payload_sha256 == "ab" * 32
    assert stamped.payload_scope_version == "html_scope@1:remax:beef"
    assert stamped.history_completeness == ARCHIVE_HISTORY_COMPLETENESS


def test_the_entry_keeps_its_published_locator_kind():
    """C9's whole point: the runtime maps the SURFACE, the contract is not rewritten."""
    entry = archive_entry(surface="embedded_json")
    assert entry.surface == "embedded_json"
    assert stamp_archive_claim(raw_claim(entry), payload(),
                               scope_version="v").surface == "archived_html"


def test_the_archive_page_kind_enum_member_stays_unused():
    with pytest.raises(IntakeRefused, match="archive"):
        stamp_archive_claim(raw_claim(), payload(page_kind="archive"), scope_version="v")


def test_the_anchor_is_the_only_one_the_check_allows_beside_a_null_snapshot():
    """`loc_claim_anchor`: snapshot_anchor='snapshot' <-> snapshot_id IS NOT NULL."""
    anchor_check = re.search(r"constraint loc_claim_anchor check \((.*?)\)\);",
                             _MIGRATION_382, re.S)
    assert anchor_check
    assert "snapshot_anchor <> 'snapshot' and snapshot_id is null" in anchor_check.group(1)
    stamped = stamp_archive_claim(raw_claim(), payload(), scope_version="v")
    assert (stamped.snapshot_anchor != "snapshot") is (stamped.snapshot_id is None)


# ------------------------------------------------------------------ the licence ladder

def test_mapy_membership_vetoes_a_coordinate_on_the_archived_substrate_too():
    """06 §6.4's gate is `claims JOIN <R2 inventory> USING (listing_id) WHERE
    claim_type='coordinate'` = 0. Re-reading the same position out of an archived page is
    the same position."""
    admitted = coordinate_verdict(
        "remax", None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rx.det.gps")
    vetoed = coordinate_verdict(
        "remax", None, in_mapy_inventory=True, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rx.det.gps")
    assert admitted.admitted and admitted.licence_class == "portal"
    assert not vetoed.admitted
    assert vetoed.reason == "listing_in_mapy_affected_inventory"


def test_remax_publishes_no_payload_coordinate_but_does_publish_an_archived_one():
    """The gap this table closes: today's `COORDINATE_RULES` describe `raw_json` only, and
    remax's answer there is 'none' while `#printMap[data-gps]` is a first-party pin."""
    assert not coordinate_verdict("remax", "page", in_mapy_inventory=False).admitted
    assert coordinate_verdict(
        "remax", None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rx.det.gps").admitted


def test_realitymix_nominatim_branch_is_odbl_never_portal():
    """C6: ODbL follows the geometry, not the republisher. `/build/maps.913b4199.js` calls
    nominatim.openstreetmap.org whenever `data-gps-*` is absent."""
    pinned = coordinate_verdict(
        "realitymix", None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rm.det.gps", portal_pin_present=True)
    geocoded = coordinate_verdict(
        "realitymix", None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rm.det.gps", portal_pin_present=False)
    assert pinned.licence_class == "portal"
    assert geocoded.admitted and geocoded.licence_class == "odbl"


def test_a_portal_with_no_geocoded_branch_refuses_a_pinless_coordinate():
    verdict = coordinate_verdict(
        "remax", None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rx.det.gps", portal_pin_present=False)
    assert not verdict.admitted
    assert verdict.reason == "coordinate_provenance_unestablished"


def test_an_unruled_locator_gets_no_coordinate():
    """A later per-portal PR cannot license a second coordinate locator by declaring
    `claim_type: coordinate`; it has to add a row to the table and argue for it."""
    verdict = coordinate_verdict(
        "remax", None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="rx.det.carousel_gps")
    assert not verdict.admitted
    assert verdict.reason == "unrecognised_archived_coordinate_locator"


@pytest.mark.parametrize("source", ["sreality", "bezrealitky", "bazos", "ceskereality"])
def test_a_portal_with_no_archived_detail_map_gets_no_archived_coordinate(source):
    verdict = coordinate_verdict(
        source, None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="whatever")
    assert not verdict.admitted


def test_the_archived_rules_name_the_five_entries_and_only_current_licence_spellings():
    assert {r.entry_id for r in ARCHIVED_COORDINATE_RULES.values()} == {
        "rx.det.gps", "rm.det.gps", "id.det.subject_feature", "mm.det.point",
        "mx.det.map_features"}
    declared = {r.licence_class for r in ARCHIVED_COORDINATE_RULES.values()}
    declared |= {r.geocoded_licence_class for r in ARCHIVED_COORDINATE_RULES.values()
                 if r.geocoded_licence_class}
    # 00 §6.2 retired first_party / portal_first_party / portal_payload / portal_osm_derived
    # / ruian_ccby; the six survivors are the `licence_class` enum's members.
    assert declared <= {"portal", "cc_by_ruian", "odbl", "commercial_permanent",
                        "ephemeral_display_only", "operator"}
    assert declared == {"portal", "odbl"}


def test_the_payload_substrate_ladder_is_byte_for_byte_unchanged():
    """Every W1/W3 call site passes no `substrate`, so the default arm must still answer
    exactly as it did."""
    assert coordinate_verdict("sreality", None, in_mapy_inventory=False).licence_class == "portal"
    assert not coordinate_verdict("bazos", "geocode", in_mapy_inventory=False).admitted
    assert coordinate_verdict("idnes", "carry_forward", in_mapy_inventory=False).admitted
    assert not coordinate_verdict("idnes", "carry_forward", in_mapy_inventory=True).admitted


# ------------------------------------------------------------------ absences

def _snapshot_key_expression() -> str:
    match = re.search(r"snapshot_key\s+bigint generated always as \((.*?)\) stored",
                      _MIGRATION_382, re.I)
    assert match
    return " ".join(match.group(1).split())


def test_an_archived_absence_carries_the_archived_surface_and_the_minus_one_sentinel():
    """`location_claim_absences` is UNIQUE on (listing_id, snapshot_key, surface, field,
    extractor_version) and `snapshot_key` is a GENERATED coalesce(snapshot_id, -1). This
    lane never has a snapshot, so -1 is the sentinel — the same one W1 lands on."""
    assert _snapshot_key_expression() == "coalesce(snapshot_id, -1)"

    entry = archive_entry(claim_type="coordinate")
    # No reader is registered, so nothing is attempted at all — the honest inert result.
    inert = extract_payload(payload(), listing_row(), [entry], register=EMPTY_REGISTER)
    assert inert.claims == [] and inert.absences == []

    absences = _absences_from_a_broken_scoper(entry)
    assert absences, "an incomplete scoper must still record the attempt"
    for absence in absences:
        assert absence.surface == ARCHIVE_SURFACE
        assert absence.snapshot_id is None
        row = absence.to_row("claims_remine_archive@1")
        assert row["snapshot_id"] is None
        assert row["surface"] == "archived_html"


def _absences_from_a_broken_scoper(entry: Entry) -> list[Any]:
    """A register whose selector will not compile makes `scope_html` fail CLOSED
    (`is_complete=False`), which is the one path that produces absences without a reader."""
    register = ScopeRegister.from_zones(
        "remax", [{"locator_kind": "html_selector", "locator": {"css": ":::not-a-selector"},
                   "reason": "test"}])
    original = dict(ARCHIVE_READERS)
    ARCHIVE_READERS["fake_html"] = lambda entry, row, payload, document: []
    try:
        return extract_payload(payload(), listing_row(), [entry], register=register).absences
    finally:
        ARCHIVE_READERS.clear()
        ARCHIVE_READERS.update(original)


def test_archived_absences_dedupe_on_the_key_the_unique_index_actually_carries():
    rows = dedupe_absence_rows([
        {"listing_id": 1, "snapshot_id": None, "surface": "archived_html",
         "field": "coordinate", "reason": "not_attempted", "extractor_version": "v"},
        {"listing_id": 1, "snapshot_id": None, "surface": "archived_html",
         "field": "coordinate", "reason": "not_stated", "extractor_version": "v"},
        {"listing_id": 1, "snapshot_id": None, "surface": "api_json",
         "field": "coordinate", "reason": "not_attempted", "extractor_version": "v"},
    ])
    # "no coordinate in the JSON" and "no coordinate in the archived HTML" are different
    # facts (`surface` is in the key); two reasons for the same surface are one row.
    assert len(rows) == 2
    assert {r["surface"] for r in rows} == {"archived_html", "api_json"}


# ------------------------------------------------------- end to end through extract_payload

def _with_reader(fn: Any, entries: list[Entry], **kwargs: Any) -> Any:
    original = dict(ARCHIVE_READERS)
    ARCHIVE_READERS["fake_html"] = fn
    try:
        return extract_payload(payload(), listing_row(**kwargs), entries,
                               register=EMPTY_REGISTER)
    finally:
        ARCHIVE_READERS.clear()
        ARCHIVE_READERS.update(original)


def test_a_readers_claim_comes_out_fully_archived_stamped():
    entry = archive_entry()
    result = _with_reader(
        lambda entry, row, payload, document: [_base(entry, row, value_text="Krymská")],
        [entry])
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert (claim.surface, claim.page_kind, claim.snapshot_anchor) == (
        "archived_html", "detail", "unanchored_latest_fetch")
    assert claim.snapshot_id is None
    assert claim.payload_id == 9001 and claim.payload_sha256 == "ab" * 32
    assert claim.payload_scope_version == EMPTY_REGISTER.scope_version
    assert claim.first_observed_at == FETCHED_AT
    assert claim.extractor_version == "contract:remax@2", "the CONTRACT's version, not the lane's"


def test_an_evidence_bearing_reader_that_forgets_its_span_takes_the_run_down():
    """Refusing loudly is the point: a reader bug must not become 1.4 M rows the CHECK
    rejects one transaction at a time."""
    entry = archive_entry(extraction_method="regex_text")
    with pytest.raises(IntakeRefused, match="loc_claim_text_evidence|payload_scope_version"):
        _with_reader(
            lambda entry, row, payload, document: [_base(entry, row, value_text="Krymská")],
            [entry])


def test_an_entry_declared_for_another_page_kind_never_runs():
    """A detail-page selector run over an index body is how a neighbour's address becomes
    the subject's."""
    result = _with_reader(
        lambda entry, row, payload, document: [_base(entry, row, value_text="Krymská")],
        [archive_entry(page_kind="index")])
    assert result.claims == []


def test_a_coordinate_from_a_listing_in_the_mapy_inventory_becomes_an_absence():
    entry = archive_entry(entry_id="rx.det.gps", claim_type="coordinate")
    result = _with_reader(
        lambda entry, row, payload, document: [
            _base(entry, row, value_geom_wkt="POINT(14.45 50.08)")],
        [entry], in_mapy_inventory=True)
    assert result.claims == []
    assert [(a.field_, a.surface, a.reason) for a in result.absences] == [
        ("coordinate", "archived_html", "not_attempted")]
    assert result.absences[0].detail == "listing_in_mapy_affected_inventory"


def test_the_ladder_stamps_the_licence_class_not_the_reader():
    """A reader that declared `portal` on realitymix's Nominatim branch is overruled: C6 is
    decided once, in the table, not once per portal reader."""
    entry = archive_entry(entry_id="rm.det.gps", source="realitymix",
                          claim_type="coordinate")
    original = dict(ARCHIVE_READERS)
    ARCHIVE_READERS["fake_html"] = lambda entry, row, payload, document: [
        _base(entry, row, value_geom_wkt="POINT(18.0 49.7)", licence_class="odbl")]
    try:
        result = extract_payload(
            payload(source="realitymix"),
            listing_row(source="realitymix"), [entry],
            register=ScopeRegister.from_zones("realitymix", ()))
    finally:
        ARCHIVE_READERS.clear()
        ARCHIVE_READERS.update(original)
    assert [c.licence_class for c in result.claims] == ["odbl"]


# ------------------------------------------------------------------ write-path lockstep

def test_to_row_and_the_recordset_column_list_stay_in_lockstep():
    """`Claim.to_row()` feeds `jsonb_to_recordset(...) AS x(...)` positionally by NAME; a
    field added to one and not the other silently writes NULL forever."""
    columns = re.search(r"jsonb_to_recordset\(%\(rows\)s::jsonb\) AS x\((.*?)\)\n",
                        claims_intake._CLAIM_WRITE_SQL, re.S)
    assert columns
    declared = re.findall(r"(\w+)\s+(?:bigint|text|timestamptz|numeric|jsonb|integer|boolean)",
                          columns.group(1))
    assert set(declared) == set(raw_claim().to_row())


def _top_level_items(expression: str) -> list[str]:
    items, depth, current = [], 0, ""
    for char in expression:
        if char == "," and depth == 0:
            items.append(current.strip())
            current = ""
            continue
        depth += (char == "(") - (char == ")")
        current += char
    if current.strip():
        items.append(current.strip())
    return items


def test_the_insert_column_list_and_its_select_have_the_same_arity():
    """Six columns were added to both halves of one INSERT … SELECT. A one-column skew
    would not be a syntax error at import time — it is a runtime `INSERT has more target
    columns than expressions`, discovered by the first batch that ever ran."""
    insert = re.search(r"INSERT INTO location_claims \((.*?)\)\s*SELECT (.*?)\s*FROM deduped",
                       claims_intake._CLAIM_WRITE_SQL, re.S)
    assert insert
    assert len(_top_level_items(insert.group(1))) == len(_top_level_items(insert.group(2)))

    observations = re.search(
        r"INSERT INTO location_claim_observations\s*\((.*?)\)\s*SELECT (.*?)\s*FROM resighted",
        claims_intake._CLAIM_WRITE_SQL, re.S)
    assert observations
    assert (len(_top_level_items(observations.group(1)))
            == len(_top_level_items(observations.group(2))))


def test_every_evidence_column_reaches_the_insert():
    for column in ("payload_id", "payload_sha256", "evidence_quote", "span_start",
                   "span_end", "payload_scope_version"):
        assert column in claims_intake._CLAIM_WRITE_SQL, column
    # bytea cannot ride in a jsonb array, so the hash is hex text until the SQL decodes it.
    assert "decode(d.payload_sha256, 'hex')" in claims_intake._CLAIM_WRITE_SQL
    assert "decode(r.payload_sha256, 'hex')" in claims_intake._CLAIM_WRITE_SQL


def test_the_fingerprint_stays_time_free_and_evidence_free():
    """01 §4.2.1: values dedupe, occurrences are their own series. An evidence span in the
    tuple would fork one claim per body."""
    for column in ("payload_sha256", "evidence_quote", "span_start", "span_end",
                   "payload_id", "snapshot_id", "first_observed_at"):
        assert column not in claims_intake._CLAIM_FINGERPRINT_SQL, column


# ------------------------------------------------------------------ chunking bounds

def _archive_row(listing_id: int, filler: int = 0) -> dict[str, Any]:
    row = raw_claim(evidence_quote="x" * filler if filler else None,
                    span_start=0 if filler else None,
                    span_end=filler if filler else None,
                    payload_sha256="ab" * 32 if filler else None).to_row()
    row["listing_id"] = listing_id
    return row


def test_the_chunk_bounds_are_the_ones_w1_shipped():
    assert DEFAULT_WRITE_CHUNK_ROWS == 5_000
    assert DEFAULT_WRITE_CHUNK_BYTES == 32 * 1024 * 1024


def test_a_listing_is_never_split_across_two_chunks_even_with_evidence_columns():
    """`claim_fingerprint`'s tuple begins with (listing_id, source, source_id_native), so
    two fingerprint-equal claims are the same listing's. Split them across statements and
    the second copy joins the `resighted` cohort and appends a spurious observation."""
    rows = [_archive_row(1), _archive_row(1), _archive_row(1), _archive_row(2)]
    chunks = list(chunk_rows(rows, max_rows=2, max_bytes=DEFAULT_WRITE_CHUNK_BYTES))
    assert [len(c) for c in chunks] == [3, 1]
    for chunk in chunks:
        assert len({r["listing_id"] for r in chunk}) == 1


def test_the_byte_budget_still_trips_on_evidence_bearing_rows():
    rows = [_archive_row(1, filler=4096), _archive_row(2, filler=4096)]
    chunks = list(chunk_rows(rows, max_rows=DEFAULT_WRITE_CHUNK_ROWS, max_bytes=5000))
    assert len(chunks) == 2


# ------------------------------------------------------------------ batch semantics

class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._result: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self._conn.dispatch(self, " ".join(sql.split()), params or {})

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._result[0] if self._result else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._result


class _Conn:
    """An in-memory `portal_raw_payloads` + `location_claim_batches` pair, keyset
    arithmetic and all — the same shape `test_claims_intake_resume` uses, because the
    invariant under test is the same one: every body seen exactly once across a chain of
    budgeted runs."""

    def __init__(self, count: int) -> None:
        self.payloads = [
            (i, "remax", f"n{i}", "detail", "ab" * 32,
             FETCHED_AT + timedelta(minutes=i), 1000 + i, False)
            for i in range(1, count + 1)
        ]
        self.batches: list[dict[str, Any]] = []
        self.seen: list[int] = []
        self.now = OBSERVED_AT

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Cursor:
        return _Cursor(self)

    def dispatch(self, cur: _Cursor, sql: str, params: dict[str, Any]) -> None:
        cur._result = []
        if "set_config" in sql:
            return
        if "FROM portal_contracts WHERE is_active" in sql:
            cur._result = [("remax", [])]
            return
        if "FROM portal_contracts WHERE source" in sql:
            cur._result = [(1, 2)]
            return
        if sql.startswith("INSERT INTO location_claim_batches"):
            self.now += timedelta(minutes=1)
            batch = {
                "id": len(self.batches) + 1, "started_at": self.now,
                "source": params["source"], "scan_mode": params["scan_mode"],
                "resumable": params["resumable"], "outcome": "running",
                "cursor_after_id": None, "cursor_after_ts": None,
                "coverage_since": params["coverage_since"] or self.now,
            }
            self.batches.append(batch)
            cur._result = [(batch["id"], batch["coverage_since"])]
            return
        if sql.startswith("UPDATE location_claim_batches"):
            batch = self.batches[params["batch_id"] - 1]
            batch["outcome"] = params["outcome"]
            batch["cursor_after_id"] = params["cursor_after_id"]
            batch["cursor_after_ts"] = params["cursor_after_ts"]
            return
        if "SELECT max(coalesce(coverage_since, started_at))" in sql:
            oks = [b["coverage_since"] for b in self.batches
                   if b["outcome"] == "ok" and b["source"] == params["source"]]
            cur._result = [(max(oks) if oks else None,)]
            return
        if "SELECT outcome, cursor_after_id, cursor_after_ts" in sql:
            candidates = [b for b in self.batches
                          if b["source"] == params["source"]
                          and b["scan_mode"] == params["scan_mode"]
                          and b["resumable"]
                          and b["outcome"] in ("ok", "stopped", "failed")]
            if candidates:
                last = max(candidates, key=lambda b: (b["started_at"], b["id"]))
                cur._result = [(last["outcome"], last["cursor_after_id"],
                                last["cursor_after_ts"], last["coverage_since"])]
            return
        if "FROM portal_raw_payloads p" in sql:
            cur._result = self._scan(sql, params)
            self.seen.extend(r[0] for r in cur._result)
            return
        if sql.startswith("SELECT id, body, body_r2_key, content_encoding"):
            cur._result = [(pid, BODY, None, "identity") for pid in params["ids"]]
            return
        if "INSERT INTO location_claims" in sql:
            cur._result = [(1, 0, 1)]
            return
        if sql.startswith("INSERT INTO location_claim_absences"):
            return
        if sql.startswith("INSERT INTO location_enrichment_state"):
            return
        raise AssertionError(f"unhandled SQL: {sql[:120]}")

    def _scan(self, sql: str, params: dict[str, Any]) -> list[tuple[Any, ...]]:
        rows = sorted(self.payloads, key=lambda r: r[0])
        if "p.first_observed_at >= %(watermark)s" in sql:
            rows = sorted(self.payloads, key=lambda r: (r[5], r[0]))
            rows = [r for r in rows
                    if r[5] >= params["watermark"]
                    and (r[5], r[0]) > (params["after_ts"], params["after_id"])]
        else:
            rows = [r for r in rows if r[0] > params["after_id"]]
        return rows[:params["batch_size"]]


@pytest.fixture
def _wired(monkeypatch: pytest.MonkeyPatch) -> None:
    """The refusal gates have their own tests; these are about the scan. One synthetic
    remax entry, one fake reader, so the lane has something to do at all."""
    entry = archive_entry()
    monkeypatch.setattr(claims_remine_archive, "missing_relations", lambda conn: [])
    monkeypatch.setattr(claims_remine_archive, "assert_inventory_ready", lambda conn: 2201)
    monkeypatch.setattr(claims_remine_archive, "load_entries", lambda conn: {"remax": [entry]})
    monkeypatch.setitem(
        ARCHIVE_READERS, "fake_html",
        lambda entry, row, payload, document: [_base(entry, row, value_text="Krymská")])


def _run(conn: _Conn, **kwargs: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "mode": "full", "source": "remax", "batch_size": 10, "max_seconds": None,
        "limit": None, "start_after_id": 0, "overlap_hours": 3, "statement_timeout": 60,
        "dry_run": False, "note": None,
    }
    defaults.update(kwargs)
    return run(conn, **defaults)


def test_a_run_with_no_registered_reader_opens_no_batch_at_all(monkeypatch):
    """Inert must not mean 'ok'. A batch that reached the end of the scan is stamped 'ok',
    and 'ok' is what the incremental watermark reads — so a lane with no readers would
    claim it had mined the whole archive, and the first portal PR would start behind a
    watermark covering bodies nothing ever looked at."""
    monkeypatch.setattr(claims_remine_archive, "missing_relations", lambda conn: [])
    monkeypatch.setattr(claims_remine_archive, "assert_inventory_ready", lambda conn: 2201)
    monkeypatch.setattr(claims_remine_archive, "load_entries",
                        lambda conn: {"remax": [archive_entry(reader="point_pair")]})
    conn = _Conn(5)
    stats = _run(conn)
    assert stats["outcome"] == "inert"
    assert conn.batches == []
    assert conn.seen == []


def test_a_budget_stopped_run_is_stamped_stopped_and_resumes_where_it_left_off(_wired):
    conn = _Conn(25)

    first = _run(conn, limit=10)
    assert first["outcome"] == "stopped"
    assert first["reached_end"] is False
    assert conn.seen == list(range(1, 11))
    assert conn.batches[0]["cursor_after_id"] == 10

    second = _run(conn, limit=10)
    assert second["resumed_from_id"] == 10
    assert second["outcome"] == "stopped"

    third = _run(conn, limit=10)
    assert third["outcome"] == "ok"
    assert third["reached_end"] is True
    assert conn.seen == list(range(1, 26))

    fourth = _run(conn, limit=10)
    assert fourth["resumed_from_id"] == 0


def test_ok_means_the_scan_exhausted_and_nothing_else(_wired):
    conn = _Conn(3)
    stats = _run(conn)
    assert stats["outcome"] == "ok"
    assert stats["payloads"] == 3
    assert conn.batches[-1]["outcome"] == "ok"


def test_the_incremental_watermark_never_passes_rows_a_stopped_run_never_opened(_wired):
    conn = _Conn(25)
    assert _run(conn, mode="full")["outcome"] == "ok"
    conn.seen.clear()

    stopped = _run(conn, mode="incremental", limit=10)
    assert stopped["outcome"] == "stopped"
    assert stopped["mode"] == "incremental"

    resumed = _run(conn, mode="incremental")
    assert resumed["outcome"] == "ok"
    assert sorted(conn.seen) == list(range(1, 26))


def test_an_operator_anchored_run_is_never_a_resume_point(_wired):
    conn = _Conn(25)
    anchored = _run(conn, start_after_id=20, limit=2)
    assert anchored["outcome"] == "stopped"
    assert conn.batches[0]["resumable"] is False
    assert conn.seen == [21, 22]

    conn.seen.clear()
    assert _run(conn, limit=5)["resumed_from_id"] == 0


def test_the_scan_reads_the_latest_body_per_key_and_never_projects_one(_wired):
    """Two properties of the scan SQL that a fake connection cannot show. The body is
    fetched separately, for the rows an entry applies to only: the archive is 14 GB of
    TOASTed text and a scan that detoasts it to discover it has no reader is the failure
    W2-0's denominator query exists to avoid."""
    for sql in (claims_remine_archive._PAYLOAD_SCAN_FULL_SQL,
                claims_remine_archive._PAYLOAD_SCAN_INCREMENTAL_SQL):
        assert "p.body" not in sql and "raw_json" not in sql
        assert "NOT EXISTS" in sql
        assert "(n.first_observed_at, n.id) > (p.first_observed_at, p.id)" in sql
        # `version_seq` was added with no backfill (403), so NULL there would rank an
        # older row as the latest; `last_observed_at` moves on an unchanged refetch.
        assert "version_seq" not in sql and "last_observed_at" not in sql
