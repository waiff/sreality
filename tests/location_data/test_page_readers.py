"""The page-reader half of the ONE claim lane — evidence discipline, the licence ladder
on the stored body, the readers themselves, and the R2 fetch.

  * EVIDENCE. Migration 382's `loc_claim_text_evidence` / `loc_claim_evidence_payload` are
    the LAST line of defence. A batch is one transaction, so a single malformed claim rolls
    back every good claim beside it — and the DB's error names a constraint, not the entry.
    `assert_evidence_complete` must refuse first, and `test_the_python_validator_requires_
    exactly_what_the_db_check_requires` reads the applied DDL so the two cannot drift.
  * THE LADDER. `mapy_affected` membership vetoes a coordinate on the page substrate
    exactly as it does on `raw_json` (06 §6.4's gate joins on `listing_id`, not on
    `surface`), and C6's licence spellings are pinned against the enum.
  * THE FETCH. Bodies live in R2; `load_bodies` is what a batch pays for, so its width, its
    per-id routing and its all-or-nothing failure are pinned here.

The lane that calls all of this is `location_data.claims_intake` (one lane, rule 25); its
scan, hash gate and write are pinned in tests/location_data/test_claims_intake_run.py.
"""

from __future__ import annotations

import re
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from location_data import claims_intake, page_readers, payloads
from location_data.claims_common import (
    ARCHIVED_COORDINATE_RULES,
    SUBSTRATE_ARCHIVED_HTML,
    Claim,
    Entry,
    IntakeRefused,
    ListingRow,
    _base,
    coordinate_verdict,
)
from location_data.claims_intake import (
    DEFAULT_WRITE_CHUNK_BYTES,
    DEFAULT_WRITE_CHUNK_ROWS,
    chunk_rows,
)
from location_data.html_scope import ScopeRegister, scope_html
from location_data.page_readers import (
    ARCHIVE_ANCHOR,
    ARCHIVE_HISTORY_COMPLETENESS,
    ARCHIVE_SURFACE,
    PAGE_READERS,
    POSITION_BRANCH_PORTAL_GEOCODED,
    POSITION_BRANCH_PORTAL_PIN,
    ArchivedPayload,
    PageRead,
    assert_evidence_complete,
    assert_stampable,
    extract_page,
    page_entries,
    stamp_page_claim,
)


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
        default_licence_class=licence_class, guards=())


def listing_row(**overrides: Any) -> ListingRow:
    kwargs: dict[str, Any] = {
        "listing_id": 4242, "source": "remax", "source_id_native": "445781",
        "raw_json": {}, "lat": None, "lon": None, "observed_at": FETCHED_AT,
        "in_mapy_inventory": False,
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
    provenance is `stamp_page_claim`'s job, which is exactly what the tests below
    exercise."""
    entry = entry or archive_entry()
    return _base(entry, listing_row(), value_text="Krymská", **overrides)


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


def _llm_claim(**overrides: Any) -> Claim:
    kwargs: dict[str, Any] = {
        "evidence_quote": "Krymská", "span_start": 10, "span_end": 17,
        "payload_scope_version": "html_scope@1:remax:deadbeefdeadbeef",
        "payload_sha256": "ab" * 32, "subject_scoped": True,
        "model": "claude-x", "prompt_version": "loc_mine@1",
    }
    kwargs.update(overrides)
    return raw_claim(archive_entry(extraction_method="llm_text"), **kwargs)


def test_an_llm_text_claim_must_name_the_model_that_made_it():
    """`loc_claim_llm_model` is the SECOND CHECK an evidence-bearing claim faces, and it
    binds `llm_text` alone. Before this, `Claim` could spell an `llm_text` claim that
    satisfied every Python guard and then violated the constraint — taking the whole batch
    with it, once, in production, on whoever built the LLM lane."""
    required = _evidence_columns_in_check("loc_claim_llm_model")
    assert required == {"model", "prompt_version"}

    assert_evidence_complete(_llm_claim())
    for column in sorted(required):
        with pytest.raises(IntakeRefused, match=column):
            assert_evidence_complete(_llm_claim(**{column: None}))


def test_a_regex_text_claim_needs_no_model():
    """The CHECK names `llm_text` and nothing else: a deterministic regex has no model to
    attribute, and demanding one would make the archived readers unbuildable."""
    assert_evidence_complete(raw_claim(
        archive_entry(extraction_method="regex_text"),
        evidence_quote="Krymská", span_start=10, span_end=17,
        payload_scope_version="v", payload_sha256="ab" * 32, subject_scoped=True))


def test_the_model_columns_reach_the_row_dict_but_no_longer_the_table():
    """W1-b dropped both columns (migration 498) — no lane emits an `llm_text` claim, so
    the CHECK that forced them was guarding a shape nothing writes. They stay on the
    `Claim` and in the recordset that parses it, because the READERS still carry the
    evidence discipline in Python; they simply have nowhere to land."""
    recordset = claims_intake._CLAIM_WRITE_SQL.split("), typed AS")[0]
    for column in ("model", "prompt_version"):
        assert column in recordset, column
        assert f"d.{column}" not in claims_intake._CLAIM_WRITE_SQL, column


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
    assert page_readers.ARCHIVE_EMITTABLE_LICENCE_CLASSES == {"portal", "odbl"}
    for value in ("ephemeral_display_only", "cc_by_ruian", "operator"):
        with pytest.raises(IntakeRefused, match="licence_class"):
            assert_stampable(raw_claim(archive_entry(licence_class=value)))


# ---------------------------------------------------------------- archived stamping (C9/C10/C4)

def test_every_claim_is_stamped_archived_html_with_the_pages_own_page_kind():
    stamped = stamp_page_claim(raw_claim(), payload(page_kind="index"),
                                  scope_version="html_scope@1:remax:beef")
    assert stamped.surface == ARCHIVE_SURFACE == "archived_html"
    assert stamped.page_kind == "index", "C10: a body does not change what kind of page it is"
    assert stamped.to_row()["snapshot_id"] is None
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
    assert stamp_page_claim(raw_claim(entry), payload(),
                               scope_version="v").surface == "archived_html"


def test_the_archive_page_kind_enum_member_stays_unused():
    with pytest.raises(IntakeRefused, match="archive"):
        stamp_page_claim(raw_claim(), payload(page_kind="archive"), scope_version="v")


def test_the_anchor_is_the_only_one_the_check_allows_beside_a_null_snapshot():
    """`loc_claim_anchor`: snapshot_anchor='snapshot' <-> snapshot_id IS NOT NULL.

    W3 (`location_data.claims_remine`) is the one lane that writes a real `snapshot_id`,
    so the shared `Claim` and `_CLAIM_WRITE_SQL` both carry the column. THIS lane's
    substrate is a latest-wins archived body with no snapshot to anchor to, so it leaves
    the column NULL — which is exactly the side of the CHECK its `unanchored_latest_fetch`
    anchor pairs with. Asserting the NULL is stronger than asserting the column's absence
    was: it pins the value that actually reaches the constraint."""
    anchor_check = re.search(r"constraint loc_claim_anchor check \((.*?)\)\);",
                             _MIGRATION_382, re.S)
    assert anchor_check
    assert "snapshot_anchor <> 'snapshot' and snapshot_id is null" in anchor_check.group(1)
    assert re.search(r"^\s*snapshot_id\s+bigint,\s*$", _MIGRATION_382, re.M), \
        "the column must be nullable for the write to legally omit it"
    stamped = stamp_page_claim(raw_claim(), payload(), scope_version="v")
    assert stamped.snapshot_anchor != "snapshot"
    assert stamped.to_row()["snapshot_id"] is None
    # The shared writer carries the column (W3 fills it); this lane's contribution to the
    # pairing is that it never stamps the 'snapshot' anchor, so its NULL is always legal.
    assert "snapshot_id" in claims_intake._CLAIM_WRITE_SQL
    assert ARCHIVE_ANCHOR != "snapshot"


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


def test_remax_admits_the_same_pin_on_both_substrates_once_it_is_stamped():
    """Until 2026-09-11 `COORDINATE_RULES["remax"]` was 'none' — the parser stamped no
    provenance, so the payload arm refused the very `#printMap[data-gps]` pin the archived
    arm admitted. The parser now stamps the subject-map pin `page`; an UNSTAMPED row (drained
    before the stamp) is still refused on the payload arm, never admitted on faith."""
    assert coordinate_verdict("remax", "page", in_mapy_inventory=False).admitted
    assert coordinate_verdict("remax", None, in_mapy_inventory=False).reason == (
        "coordinate_provenance_unestablished")
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


@pytest.mark.parametrize("source", ["sreality", "bezrealitky", "ceskereality"])
def test_a_portal_with_no_archived_detail_map_gets_no_archived_coordinate(source):
    verdict = coordinate_verdict(
        source, None, in_mapy_inventory=False, substrate=SUBSTRATE_ARCHIVED_HTML,
        entry_id="whatever")
    assert not verdict.admitted


def test_the_archived_rules_name_the_seven_entries_and_only_current_licence_spellings():
    """bazos joined on 2026-09-12 (W1-c R6): the ad's own
    `google.com/maps/place/<lat>,<lon>` anchor is the PAGE publishing a pin, so it is
    first-party exactly as remax's `#printMap[data-gps]` is. That the pin is permanently
    approximate is the entry's `precision_cap`, not a missing row here — a missing row says
    "this portal publishes no coordinate at all", which was never true. ceskereality joined
    with it (W1-c R10): `input#driving_calculator_from`'s decimal pair is the portal's own,
    and once rule 25 deleted the `listings.geom` reader it is the only pin it has."""
    assert {r.entry_id for r in ARCHIVED_COORDINATE_RULES.values()} == {
        "rx.det.gps", "rm.det.gps", "id.det.subject_feature", "mm.det.point",
        "mx.det.map_features", "bzs.det.link_pin", "cr.det.page_pin"}
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


# ------------------------------------------------------- end to end through extract_page

def _with_reader(fn: Any, entries: list[Entry], *, max_value_bytes: int | None = None,
                 **kwargs: Any) -> Any:
    original = dict(PAGE_READERS)
    PAGE_READERS["fake_html"] = fn
    try:
        return extract_page(payload(), listing_row(**kwargs), entries,
                               register=EMPTY_REGISTER,
                               max_value_bytes=max_value_bytes)
    finally:
        PAGE_READERS.clear()
        PAGE_READERS.update(original)


def test_a_readers_claim_comes_out_fully_archived_stamped():
    entry = archive_entry()
    result = _with_reader(
        lambda entry, row, payload, document: [PageRead(_base(entry, row, value_text="Krymská"))],
        [entry])
    assert len(result.claims) == 1
    claim = result.claims[0]
    assert (claim.surface, claim.page_kind, claim.snapshot_anchor) == (
        "archived_html", "detail", "unanchored_latest_fetch")
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
            lambda entry, row, payload, document: [PageRead(_base(entry, row, value_text="Krymská"))],
            [entry])


def test_an_entry_declared_for_another_page_kind_never_runs():
    """A detail-page selector run over an index body is how a neighbour's address becomes
    the subject's."""
    result = _with_reader(
        lambda entry, row, payload, document: [PageRead(_base(entry, row, value_text="Krymská"))],
        [archive_entry(page_kind="index")])
    assert result.claims == []


def test_a_coordinate_from_a_listing_in_the_mapy_inventory_becomes_a_refusal():
    entry = archive_entry(entry_id="rx.det.gps", claim_type="coordinate")
    result = _with_reader(
        lambda entry, row, payload, document: [
            PageRead(_base(entry, row, value_geom_wkt="POINT(14.45 50.08)"),
                        position_branch=POSITION_BRANCH_PORTAL_PIN)],
        [entry], in_mapy_inventory=True)
    assert result.claims == []
    assert dict(result.refusals) == {"listing_in_mapy_affected_inventory": 1}


def _realitymix_coordinate(branch: str | None, licence_class: str = "portal"):
    entry = archive_entry(entry_id="rm.det.gps", source="realitymix",
                          claim_type="coordinate")
    original = dict(PAGE_READERS)
    PAGE_READERS["fake_html"] = lambda entry, row, payload, document: [
        PageRead(_base(entry, row, value_geom_wkt="POINT(18.0 49.7)",
                          licence_class=licence_class),
                    position_branch=branch)]
    try:
        return extract_page(
            payload(source="realitymix"),
            listing_row(source="realitymix"), [entry],
            register=ScopeRegister.from_zones("realitymix", ()))
    finally:
        PAGE_READERS.clear()
        PAGE_READERS.update(original)


def test_the_ladder_stamps_the_licence_class_and_the_reader_cannot_overrule_it():
    """C6 is decided once, in `ARCHIVED_COORDINATE_RULES`, not once per portal reader.

    This is the exact shape the old inference got wrong: the reader left `'portal'` on the
    claim (the contract entry's default — what a reader that says nothing produces) while
    declaring it read the Nominatim branch. The branch decides, so the position is filed
    `'odbl'`."""
    result = _realitymix_coordinate(POSITION_BRANCH_PORTAL_GEOCODED, licence_class="portal")
    assert [c.licence_class for c in result.claims] == ["odbl"]


def test_the_pin_branch_is_first_party_even_if_the_reader_stamped_odbl():
    """And symmetrically: a reader cannot licence-launder in the other direction either."""
    result = _realitymix_coordinate(POSITION_BRANCH_PORTAL_PIN, licence_class="odbl")
    assert [c.licence_class for c in result.claims] == ["portal"]


@pytest.mark.parametrize("branch", [None, "portal_pin_blurred", ""])
def test_a_coordinate_read_that_does_not_declare_its_branch_is_refused(branch):
    """The failure this closes: a Nominatim-fallback reader that simply forgets to say so
    would inherit the entry's `licence_class: portal` default and file a republished OSM
    position as first-party, with nothing anywhere to catch it. A required argument cannot
    be forgotten quietly — and the refusal names the entry."""
    with pytest.raises(IntakeRefused, match="position_branch") as excinfo:
        _realitymix_coordinate(branch)
    assert "rm.det.gps" in str(excinfo.value)


def test_a_branch_declared_on_a_non_coordinate_read_is_refused():
    """It is a fact about a POSITION's licence lineage; on a street name it is noise, and
    noise in a required field is how the field stops being read."""
    with pytest.raises(IntakeRefused, match="position_branch"):
        _with_reader(
            lambda entry, row, payload, document: [
                PageRead(_base(entry, row, value_text="Krymská"),
                            position_branch=POSITION_BRANCH_PORTAL_PIN)],
            [archive_entry()])


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
    """The two halves of one INSERT … SELECT must stay the same length. A one-column skew
    would not be a syntax error at import time — it is a runtime `INSERT has more target
    columns than expressions`, discovered by the first batch that ever ran. W1-b narrowed
    both halves from 42 columns to 18 in one edit, which is exactly the shape this pins."""
    insert = re.search(r"INSERT INTO location_claims \((.*?)\)\s*SELECT (.*?)\s*FROM deduped",
                       claims_intake._CLAIM_WRITE_SQL, re.S)
    assert insert
    assert len(_top_level_items(insert.group(1))) == len(_top_level_items(insert.group(2)))

    assert len(_top_level_items(insert.group(1))) == 18

    # The re-sight observation CTE went with its table (rule 25): 263 M rows nobody read.
    assert "location_claim_observations" not in claims_intake._CLAIM_WRITE_SQL


def test_the_evidence_columns_reach_the_row_dict_but_no_longer_the_table():
    """Same shape as the model pair. The D7 span discipline is enforced in Python by
    `assert_evidence_complete` (the tests above); migration 498 dropped the six columns it
    used to land in, along with the two CHECKs that mirrored it — the archive the spans
    index into is `portal_raw_payloads`, which is untouched."""
    recordset = claims_intake._CLAIM_WRITE_SQL.split("), typed AS")[0]
    for column in ("payload_id", "payload_sha256", "evidence_quote", "span_start",
                   "span_end", "payload_scope_version"):
        assert column in recordset, column
        assert f"d.{column}" not in claims_intake._CLAIM_WRITE_SQL, column


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



# ------------------------------------------------------------------ where the bodies live

class _FakeStore:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.gets: list[str] = []

    def download_bytes(self, key: str) -> bytes:
        self.gets.append(key)
        return self.objects[key]


class _BodyCursor:
    """Just enough cursor to answer `_PAYLOAD_BODIES_SQL` with a canned row set."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        assert sql is page_readers._PAYLOAD_BODIES_SQL

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


def test_a_body_that_lives_in_r2_is_fetched_and_decoded_not_counted_and_skipped():
    """W2a made the bucket the bodies' HOME: the R2 threshold is Postgres's own ~2 KB TOAST
    boundary now, not the 256 KB it shipped at, so `body IS NULL AND body_r2_key IS NOT
    NULL` holds on essentially every row. A lane that skipped those would mine an empty
    corpus and stamp its batch 'ok'."""
    gzipped, encoding = payloads.encode_body(BODY * 200)
    assert encoding == "gzip"
    store = _FakeStore({"payloads/remax/ab/abcd.gz": gzipped})
    cursor = _BodyCursor([
        (1, BODY, None, "identity"),
        (2, None, "payloads/remax/ab/abcd.gz", "gzip"),
    ])
    bodies, from_r2 = page_readers.load_bodies(cursor, [1, 2], store=store)
    assert bodies == {1: BODY, 2: BODY * 200}
    assert from_r2 == 1
    assert store.gets == ["payloads/remax/ab/abcd.gz"]


def test_a_spilled_body_with_no_object_store_takes_the_run_down():
    """The 2026-08 lesson twice over (#1074/#1075): a lane whose credentials are absent must
    fail, not quietly cover a fraction of its corpus and report success."""
    cursor = _BodyCursor([(2, None, "payloads/remax/ab/abcd.gz", "gzip")])
    with pytest.raises(IntakeRefused, match="R2"):
        page_readers.load_bodies(cursor, [2], store=None)


def test_an_unconfigured_bucket_skips_the_page_half_and_never_refuses(monkeypatch):
    """R2 is where the bodies live, so the page half needs it — and the PAYLOAD half is the
    hourly ingest for all nine portals. A rotated credential must cost us the first, never
    the second: `_open_body_store` warns once and returns None, and the scan runs on."""
    monkeypatch.setattr(payloads, "open_store", lambda: None)
    assert claims_intake._open_body_store(page_capable=True) is None


def test_a_bucket_that_raises_on_open_still_leaves_the_payload_half_running(monkeypatch):
    def boom() -> None:
        raise RuntimeError("R2_ACCESS_KEY_ID is malformed")

    monkeypatch.setattr(payloads, "open_store", boom)
    assert claims_intake._open_body_store(page_capable=True) is None


# ------------------------------------------------------------------ the value-size bound

def test_an_oversized_archived_value_is_refused_and_recorded_never_dropped():
    """W1's cap exists because a reader that stores its node verbatim inherits whatever the
    substrate hands it — and an HTML reader over a whole page is exactly that producer."""
    entry = archive_entry()
    result = _with_reader(
        lambda entry, row, payload, document: [
            PageRead(_base(entry, row, value_text="x" * 4096))],
        [entry], max_value_bytes=1024)
    assert result.claims == []
    assert dict(result.refusals) == {"oversized_value:street_name": 1}
    # A counter and a log line, never a row: re-reading a content-addressed body yields the
    # same bytes forever, so a refetch enrolment would be a permanently-failing counter and
    # an absence row would be one more row in a table nothing reads (rule 25).


def test_the_evidence_quote_counts_toward_the_bound():
    """It rides in the SAME jsonb array as the value columns (`Claim.to_row()`), it is NULL
    on every W1 claim so `claim_value_bytes` never had to count it, and on this substrate it
    is a span of a 41-245 KB HTML body — the one field most likely to blow the bound."""
    claim = raw_claim(evidence_quote="q" * 500, span_start=0, span_end=500,
                      payload_sha256="ab" * 32)
    assert claims_intake.claim_value_bytes(claim) < 500
    assert page_readers.archived_claim_value_bytes(claim) >= 500
    assert (page_readers.archived_claim_value_bytes(claim)
            == claims_intake.claim_value_bytes(claim) + 500)


def test_a_value_inside_the_cap_is_kept():
    result = _with_reader(
        lambda entry, row, payload, document: [
            PageRead(_base(entry, row, value_text="Krymská"))],
        [archive_entry()], max_value_bytes=1024)
    assert len(result.claims) == 1 and not result.refusals


# --------------------------------------- the DOM readers, against the real remax fixture

_REMAX_HTML = _ROOT / "tests" / "fixtures" / "location_w2" / "remax_detail.html"


def remax_document():
    """The pinned remax detail page, scoped by remax's OWN exclusion zones.

    That page is the contamination case in one artefact: the subject is on Pod Slovany in
    Úvaly, and below it sit `.area-listings__item` neighbour cards for Oleška and Stará
    Boleslav, each carrying its own `data-address` and `data-gps`. Reading the first
    `data-address` in the document is how a neighbour's street reached `listings.street` on
    live rows (W0 item 0d) — the failure these readers exist not to reproduce.
    """
    from location_data import contracts
    contract = {c.source: c for c in contracts.load_all()}["remax"]
    register = ScopeRegister.from_zones("remax", contract.exclusion_zones)
    return scope_html(_REMAX_HTML.read_bytes(), register=register)


def dom_entry(reader: str, **locator: Any) -> Entry:
    entry = archive_entry(entry_id=f"rx.det.{reader}", reader=reader)
    return replace(entry, locator={"reader": reader, **locator})


def test_html_text_reads_the_subject_header_and_not_a_neighbour_card():
    document = remax_document()
    entry = dom_entry("html_text", css="h2.pd-header__address")
    reads = PAGE_READERS["html_text"](entry, listing_row(), payload(), document)

    # W2-6 replaced this fixture's hand-written one-line header with the real archived
    # block, so the DEEP read now states what it really states on a live remax page: the
    # subject's line, the source line-break's tab run, and the nested jump-link's own
    # label. What this test asserts is unchanged — the SELECTOR reaches the subject and
    # never a neighbour card; that the value needs `html_own_text` to be usable is the
    # next test's subject.
    assert len(reads) == 1
    value = reads[0].claim.value_text
    assert value.startswith("ulice Pod Slovany,") and value.endswith("Úvaly mapa")
    body = _REMAX_HTML.read_text(encoding="utf-8", errors="replace")
    assert "Oleška" in body and "Stará Boleslav" in body   # the decoys are really there
    assert "Oleška" not in value


def test_html_point_dms_reads_the_subject_map_and_converts_the_pair():
    document = remax_document()
    entry = dom_entry("html_point_dms", css="#printMap[data-gps], #listingMap[data-gps]",
                      attr="data-gps", position_branch=POSITION_BRANCH_PORTAL_PIN)
    reads = PAGE_READERS["html_point_dms"](entry, listing_row(), payload(), document)

    # 50°04'26.1"N,14°43'41.5"E — the SUBJECT's pin. The neighbour card's
    # 49°59'01.5"N,14°54'28.4"E is a different place entirely.
    assert len(reads) == 1
    assert reads[0].claim.value_geom_wkt == "POINT(14.728194444444444 50.07391666666667)"
    assert reads[0].position_branch == POSITION_BRANCH_PORTAL_PIN


def test_a_dom_read_carries_an_evidence_quote_and_a_span_that_contains_it():
    document = remax_document()
    # `html_own_text`, because W2-6 put the real nested header into this fixture: the deep
    # read's value ("… Úvaly mapa") is not contiguous in the source — the link's tag sits
    # between the two words — so it resolves to no span at all, which is the opposite of
    # what this test is about.
    entry = dom_entry("html_own_text", css="h2.pd-header__address")
    claim = PAGE_READERS["html_own_text"](
        entry, listing_row(), payload(), document)[0].claim

    assert claim.evidence_quote == "ulice Pod Slovany, Úvaly"
    assert claim.span_start is not None and claim.span_end > claim.span_start
    # 382's `loc_claim_text_evidence` is a SUBSTRING check against the scoped body, so the
    # span has to land on bytes that really carry the quote.
    assert "Pod Slovany" in document.html[claim.span_start:claim.span_end]


def test_a_selector_matching_nothing_yields_no_claim_rather_than_an_empty_one():
    document = remax_document()
    entry = dom_entry("html_text", css="div.no-such-node")
    assert PAGE_READERS["html_text"](entry, listing_row(), payload(), document) == []


def test_a_dom_entry_without_a_selector_is_refused_and_names_itself():
    document = remax_document()
    entry = dom_entry("html_text")
    with pytest.raises(IntakeRefused, match="locator.css"):
        PAGE_READERS["html_text"](entry, listing_row(), payload(), document)


def test_a_coordinate_read_without_a_position_branch_is_refused():
    """C6: which branch of the map produced a pin IS its licence class, so a coordinate read
    that does not state one is refused rather than silently defaulted to first-party."""
    document = remax_document()
    entry = dom_entry("html_point_dms", css="#printMap[data-gps]", attr="data-gps")
    with pytest.raises(IntakeRefused, match="position_branch"):
        PAGE_READERS["html_point_dms"](entry, listing_row(), payload(), document)


# ------------------------------- html_point_attrs, against the real realitymix fixture

_REALITYMIX_HTML = _ROOT / "tests" / "fixtures" / "location_w2" / "realitymix_detail.html"


def realitymix_document():
    """The pinned realitymix detail page, scoped by its own exclusion zones.

    The fixture is MODELLED on the contract rather than captured live (its own header says
    so); the attribute names are corroborated by `rm.det.legacy_pin`'s notes and the
    portal-verification sweep, which is what makes it usable evidence for the reader's
    shape rather than for the portal's current markup."""
    from location_data import contracts
    contract = {c.source: c for c in contracts.load_all()}["realitymix"]
    register = ScopeRegister.from_zones("realitymix", contract.exclusion_zones)
    return scope_html(_REALITYMIX_HTML.read_bytes(), register=register)


def latlon_entry(**locator: Any) -> Entry:
    base = {
        "reader": "html_point_attrs",
        "css": "div#print-map",
        "attr": ["data-gps-lat", "data-gps-lon"],
        "position_branch": POSITION_BRANCH_PORTAL_PIN,
    }
    base.update(locator)
    entry = archive_entry(entry_id="rm.det.gps", source="realitymix",
                          reader="html_point_attrs", claim_type="coordinate")
    return replace(entry, locator=base, guards=("reject_outside_cz_bbox",))


def test_html_point_attrs_reads_a_split_decimal_pair():
    """realitymix publishes `data-gps-lat` / `data-gps-lon` as SEPARATE decimal attributes,
    which is why `html_point_dms` cannot read it — that reader parses one DMS string. This
    is the case the W2-7 portal verification found."""
    reads = PAGE_READERS["html_point_attrs"](
        latlon_entry(), listing_row(source="realitymix"), payload(source="realitymix"),
        realitymix_document())

    assert len(reads) == 1
    # WKT is POINT(lon lat) — the axis order is the one thing a coordinate reader must not
    # get backwards, and 49.7N/13.4E is Plzeň while 13.4N/49.7E is the Indian Ocean.
    assert reads[0].claim.value_geom_wkt == "POINT(13.39051 49.73561)"
    assert reads[0].position_branch == POSITION_BRANCH_PORTAL_PIN


def test_the_cz_bbox_guard_is_actually_EVALUATED_not_merely_declared():
    """`html_point_attrs` declares `consults_guards=True` and must therefore CALL
    `guard_admits` — the misdeclaration an adversarial review caught on `html_point_dms`,
    which declared True while never calling it. A decimal pair gets no bbox check for free
    (unlike the DMS path, where `parse_dms_pair` applies the envelope itself), so this is
    the reader where the guard is load-bearing rather than incidental."""
    html = _REALITYMIX_HTML.read_text(encoding="utf-8", errors="replace")
    paris = html.replace('data-gps-lat="49.73561"', 'data-gps-lat="48.8566"').replace(
        'data-gps-lon="13.39051"', 'data-gps-lon="2.3522"')
    from location_data import contracts
    contract = {c.source: c for c in contracts.load_all()}["realitymix"]
    register = ScopeRegister.from_zones("realitymix", contract.exclusion_zones)
    document = scope_html(paris.encode("utf-8"), register=register)

    reads = PAGE_READERS["html_point_attrs"](
        latlon_entry(), listing_row(source="realitymix"), payload(source="realitymix"),
        document)
    assert reads == []          # outside the CZ envelope -> refused by the guard

    # And with the guard NOT declared, the same point is admitted — which proves the
    # emptiness above came from the guard rather than from the parse failing.
    unguarded = replace(latlon_entry(), guards=())
    assert len(PAGE_READERS["html_point_attrs"](
        unguarded, listing_row(source="realitymix"), payload(source="realitymix"),
        document)) == 1


def test_the_evidence_quote_is_locatable_in_the_body():
    """A coordinate assembled from two attributes has a readable value ("lat,lon") that
    appears NOWHERE in the HTML, so quoting it would produce an unlocatable span — a claim
    asserting evidence it cannot point at. The quote is therefore the node's own
    serialisation, which does contain both attributes."""
    document = realitymix_document()
    claim = PAGE_READERS["html_point_attrs"](
        latlon_entry(), listing_row(source="realitymix"), payload(source="realitymix"),
        document)[0].claim

    assert claim.value_text == "49.73561,13.39051"          # readable
    assert claim.span_start is not None and claim.span_end > claim.span_start
    span = document.html[claim.span_start:claim.span_end]
    assert "49.73561" in span and "13.39051" in span        # 382's substring check holds


def test_a_malformed_attr_pair_is_refused_rather_than_guessed():
    """The [lat, lon] ORDER is contract data because nothing in the markup states it.
    Silently accepting one name, or three, is how a coordinate lands in the wrong
    hemisphere."""
    document = realitymix_document()
    for bad in (["data-gps-lat"], ["a", "b", "c"], "data-gps-lat", []):
        with pytest.raises(IntakeRefused, match="ordered \\[lat_attr, lon_attr\\] pair"):
            PAGE_READERS["html_point_attrs"](
                latlon_entry(attr=bad), listing_row(source="realitymix"),
                payload(source="realitymix"), document)


def test_a_non_numeric_attribute_yields_no_claim_and_no_exception():
    """The portal changing shape under us must not abort a batch of thousands."""
    html = _REALITYMIX_HTML.read_text(encoding="utf-8", errors="replace").replace(
        'data-gps-lat="49.73561"', 'data-gps-lat="nope"')
    from location_data import contracts
    contract = {c.source: c for c in contracts.load_all()}["realitymix"]
    register = ScopeRegister.from_zones("realitymix", contract.exclusion_zones)
    document = scope_html(html.encode("utf-8"), register=register)
    assert PAGE_READERS["html_point_attrs"](
        latlon_entry(), listing_row(source="realitymix"), payload(source="realitymix"),
        document) == []


def test_a_non_finite_coordinate_is_refused_even_with_no_guard_declared():
    """`float()` returns nan/inf happily, and `POINT(nan nan)` reaching ST_GeomFromText
    either stores a non-finite geometry in an append-only table or aborts the whole batch
    INSERT around it. Finiteness is therefore STRUCTURAL here, not contract-declared: the
    CZ envelope is policy an entry may legitimately omit (a portal could publish foreign
    coordinates), but a NaN is never a coordinate.

    Asserted with `guards=()` precisely so it cannot pass by accident via the bbox check —
    this is the path an entry that forgot the guard would take."""
    from location_data import contracts
    contract = {c.source: c for c in contracts.load_all()}["realitymix"]
    register = ScopeRegister.from_zones("realitymix", contract.exclusion_zones)
    base = _REALITYMIX_HTML.read_text(encoding="utf-8", errors="replace")

    for bad in ("nan", "inf", "-inf"):
        html = base.replace('data-gps-lat="49.73561"', f'data-gps-lat="{bad}"')
        document = scope_html(html.encode("utf-8"), register=register)
        entry = replace(latlon_entry(), guards=())        # no guard declared at all
        assert PAGE_READERS["html_point_attrs"](
            entry, listing_row(source="realitymix"), payload(source="realitymix"),
            document) == [], bad


# ------------------------------------------------------------------ the fetch runs wide

class _BarrierStore:
    """Every download blocks until `parties` of them are in flight at once. Serial fetching
    cannot satisfy that, so a regression to one-at-a-time TIMES OUT rather than passing
    slowly — which is the only way to assert concurrency without asserting on a clock."""

    def __init__(self, objects: dict[str, bytes], parties: int) -> None:
        self.objects = objects
        self.barrier = threading.Barrier(parties, timeout=10)
        self.peak = 0
        self._live = 0
        self._lock = threading.Lock()

    def download_bytes(self, key: str) -> bytes:
        with self._lock:
            self._live += 1
            self.peak = max(self.peak, self._live)
        try:
            self.barrier.wait()
        finally:
            with self._lock:
                self._live -= 1
        return self.objects[key]


def _spilled_rows(count: int) -> tuple[list[tuple[Any, ...]], dict[str, bytes]]:
    rows: list[tuple[Any, ...]] = []
    objects: dict[str, bytes] = {}
    for i in range(count):
        key = f"payloads/remax/{i:02d}/body.html"
        objects[key] = BODY + str(i).encode()
        rows.append((i, None, key, "identity"))
    return rows, objects


def test_a_batch_of_bodies_is_fetched_concurrently_not_one_at_a_time():
    rows, objects = _spilled_rows(4)
    store = _BarrierStore(objects, parties=4)
    bodies, from_r2 = page_readers.load_bodies(
        _BodyCursor(rows), list(range(4)), store=store, workers=4)
    assert from_r2 == 4
    assert bodies == {i: objects[f"payloads/remax/{i:02d}/body.html"] for i in range(4)}
    assert store.peak == 4


def test_every_body_lands_on_its_own_payload_id_whatever_order_they_arrive_in():
    """`pool.map` yields in submission order but the DOWNLOADS finish in any order; the id
    travels WITH the bytes so a slow object cannot be filed under a fast one's row."""
    rows, objects = _spilled_rows(6)

    class _ReverseStore:
        def __init__(self) -> None:
            self.gate = threading.Event()
            self.seen: list[str] = []

        def download_bytes(self, key: str) -> bytes:
            # The first key submitted returns LAST: it waits for the others to arrive.
            if key.endswith("00/body.html"):
                assert self.gate.wait(timeout=10)
            else:
                self.seen.append(key)
                if len(self.seen) == 5:
                    self.gate.set()
            return objects[key]

    store = _ReverseStore()
    bodies, from_r2 = page_readers.load_bodies(
        _BodyCursor(rows), list(range(6)), store=store, workers=6)
    assert from_r2 == 6
    for i in range(6):
        assert bodies[i] == objects[f"payloads/remax/{i:02d}/body.html"]


@pytest.mark.parametrize("workers", [1, 5])
def test_one_bad_object_costs_one_listings_page_entries_never_the_batch(workers):
    """THE WEDGE THIS CLOSES. The batch is ONE transaction, so a single 404/timeout/decode
    error propagating out of the fan-out would roll back the PAYLOAD claims computed beside
    it — sreality's and bezrealitky's, over a remax body — stamp the batch `failed`, leave
    the watermark (which reads `outcome='ok'` only) where it was, and hand the next hourly
    run the same immutable object to die on again. Forever, for a body that is simply gone.

    So the failure is per OBJECT: warned, dropped from the result, and the caller finds no
    body for that id. Asserted on BOTH paths — the serial one and the pool — because the
    pool's `map` re-raises on consumption and only a returning `fetch` avoids that."""
    rows, objects = _spilled_rows(5)

    class _FlakyStore:
        def download_bytes(self, key: str) -> bytes:
            if key.endswith("03/body.html"):
                raise OSError("R2 timed out")
            return objects[key]

    bodies, from_r2 = page_readers.load_bodies(
        _BodyCursor(rows), list(range(5)), store=_FlakyStore(), workers=workers)

    # The four readable objects are all there; the fifth is absent, not empty — the caller
    # leaves it UNSTAMPED and the next run asks for it again.
    assert sorted(bodies) == [0, 1, 2, 4]
    assert 3 not in bodies
    for i in (0, 1, 2, 4):
        assert bodies[i] == objects[f"payloads/remax/{i:02d}/body.html"]
    # `from_r2` counts what was ATTEMPTED, so the run log still shows the fetch happening.
    assert from_r2 == 5


def test_a_spilled_row_with_no_store_at_all_still_raises():
    """The distinction the swallow keeps. A bad OBJECT is a fact about one page; NO STORE
    is a misconfigured lane, and mining only the database-resident rows would report
    coverage over a corpus that is almost entirely in the bucket."""
    rows, _ = _spilled_rows(1)
    with pytest.raises(IntakeRefused, match="no object store is configured"):
        page_readers.load_bodies(_BodyCursor(rows), [0], store=None)


def test_the_width_is_bounded_by_the_batch_and_one_worker_stays_serial():
    rows, objects = _spilled_rows(3)
    store = _FakeStore(objects)
    bodies, from_r2 = page_readers.load_bodies(
        _BodyCursor(rows), [0, 1, 2], store=store, workers=1)
    assert from_r2 == 3 and len(bodies) == 3
    # Serial: the gets keep the row order, which is what the single-worker path promises.
    assert store.gets == [f"payloads/remax/{i:02d}/body.html" for i in range(3)]
    # A width wider than the batch never spawns idle threads: one row, one worker, and the
    # barrier below would deadlock if a second thread were started.
    single_rows, single_objects = _spilled_rows(1)
    solo = _BarrierStore(single_objects, parties=1)
    bodies, from_r2 = page_readers.load_bodies(
        _BodyCursor(single_rows), [0], store=solo, workers=16)
    assert from_r2 == 1 and solo.peak == 1


def test_a_corrupt_inline_body_costs_one_listing_not_the_run():
    """The same rule as the R2 path, which the inline branch was missing: a truncated gzip
    member or a mis-stamped `content_encoding` on ONE database-resident row would raise out
    of `load_bodies`, roll back every portal's payload claims computed in the same
    transaction, and hand the next run the same immutable row to die on. It is dropped from
    the result instead — absent, not empty — so that body is left UNSTAMPED and retried."""
    cursor = _BodyCursor([
        (1, BODY, None, "identity"),
        (2, b"not-actually-gzip", None, "gzip"),
        (3, BODY, None, "identity"),
    ])
    bodies, from_r2 = page_readers.load_bodies(cursor, [1, 2, 3], store=None)
    assert sorted(bodies) == [1, 3] and from_r2 == 0


def test_an_inline_body_needs_no_store_even_when_the_batch_is_wide(monkeypatch):
    """The database-resident rows are decoded on the spot; only spilled rows reach the pool,
    so a fully inline batch still runs with no credentials at all."""
    monkeypatch.setenv(payloads.BODY_FETCH_WORKERS_ENV, "16")
    cursor = _BodyCursor([(1, BODY, None, "identity"), (2, BODY, None, "identity")])
    bodies, from_r2 = page_readers.load_bodies(cursor, [1, 2], store=None)
    assert bodies == {1: BODY, 2: BODY} and from_r2 == 0


# ------------------------------------------- the precision label (W1-c R5, 2026-09-12)

_PRECISION_BODY = (
    '<html><body>'
    '<div id="subject">Poloha na mapě je přibližná</div>'
    '<script id="cfg" type="application/json">{"geometry": {"type": "Circle"}}</script>'
    "</body></html>"
).encode("utf-8")


def _precision_claim(reader: str, method: str = "portal_declared_quality",
                     **locator: Any) -> Claim:
    """One reader hit on `_PRECISION_BODY`, stamped the way the lane stamps it."""
    document = scope_html(_PRECISION_BODY, register=EMPTY_REGISTER)
    entry = replace(
        archive_entry(claim_type="precision_declaration", reader=reader,
                      extraction_method=method),
        locator={"reader": reader, **locator})
    reads = PAGE_READERS[reader](entry, listing_row(), payload(body=_PRECISION_BODY),
                                 document)
    assert len(reads) == 1, reader
    return stamp_page_claim(reads[0].claim, payload(body=_PRECISION_BODY),
                            scope_version=document.scope_version)


def test_a_precision_declarations_label_is_its_value_whatever_reader_produced_it():
    """W1-c R5. The portal's precision signal is a different STRING on every portal and is
    lifted by a different reader — a JSON field here, a regex over a sentence there — and
    the resolver reads exactly one column for it. Stamping the label in each reader is how
    six of them ended up not stamping it at all: a claim that says "přibližná" in
    `value_text` and NULL in `declared_precision_label` reads as a portal that declares
    nothing, and `precision_cap.blurred_labels` (the contract's own calibration) then
    matches nothing either."""
    from_json = _precision_claim("json_scalar", css="#cfg", json_pointer="/geometry/type")
    assert from_json.value_text == "Circle"
    assert from_json.declared_precision_label == "Circle"

    from_regex = _precision_claim(
        "html_regex", "regex_text", css="#subject", pattern=r"je\s+(\w+)", group=1)
    assert from_regex.value_text == "přibližná"
    assert from_regex.declared_precision_label == "přibližná"


def test_a_reader_that_decided_the_label_itself_keeps_it():
    """`json_geometry` types a Circle as `circle` rather than echoing the portal's spelling,
    and `json_bool` maps a boolean to the label the CONTRACT names. Neither is overwritten:
    the rule fills a hole, it does not relitigate a reader's answer."""
    document = scope_html(_PRECISION_BODY, register=EMPTY_REGISTER)
    claim = replace(
        raw_claim(archive_entry(claim_type="precision_declaration"),
                  declared_precision_label="accurate"),
        value_text="true")
    stamped = stamp_page_claim(claim, payload(body=_PRECISION_BODY),
                               scope_version=document.scope_version)
    assert stamped.declared_precision_label == "accurate"


def test_a_claim_of_any_other_type_gets_no_label():
    document = scope_html(_PRECISION_BODY, register=EMPTY_REGISTER)
    stamped = stamp_page_claim(raw_claim(), payload(body=_PRECISION_BODY),
                               scope_version=document.scope_version)
    assert stamped.claim_type == "street_name"
    assert stamped.declared_precision_label is None
