"""The write is bounded in BYTES, not just in rows — and an unwritable value is refused.

Production failure this pins (Actions run 31482522487, hourly incremental):
`psycopg.errors.ProgramLimitExceeded: total size of jsonb array elements exceeds the
maximum of 268435455 bytes`. Every write in `claims_intake` hands ONE jsonb array to
`jsonb_to_recordset`, and a 20 000-listing sreality batch is ~378 MB of array. No database
here: the failure is in the SHAPE of the parameter, which a fake cursor can see exactly.
"""

from __future__ import annotations

import json

import pytest

from location_data.claims_intake import (
    DEFAULT_MAX_CLAIM_VALUE_BYTES,
    DEFAULT_WRITE_CHUNK_BYTES,
    DEFAULT_WRITE_CHUNK_ROWS,
    MAX_CLAIM_VALUE_BYTES_ENV,
    WRITE_CHUNK_BYTES_ENV,
    WRITE_CHUNK_ROWS_ENV,
    Absence,
    Claim,
    EnrichmentTask,
    IntakeResult,
    chunk_rows,
    claim_value_bytes,
    dedupe_enrichment_rows,
    extract_listing,
    write_result,
)
from tests.location_data.claim_intake_fixtures import (
    OBSERVED_AT,
    SREALITY_LEGACY,
    SREALITY_POST_CUTOVER,
    entries_for,
    listing,
)

# Migration 382's CHECK on `location_claim_absences.reason`, transcribed. A refusal must
# pick from this set: there is no `oversized_payload` label and adding one is DDL.
ABSENCE_REASONS = frozenset(
    {"not_stated", "stated_but_ambiguous", "only_in_excluded_block", "not_attempted"})


class _Cursor:
    """Records what was executed. `fetchone` answers the claim write's three counters."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: dict | None = None) -> None:
        self.executed.append((" ".join(sql.split()), params or {}))

    def fetchone(self) -> tuple[int, int, int]:
        rows = self.executed[-1][1]["rows"].obj
        return (len(rows), 1, 1)

    def arrays(self, table: str) -> list[list[dict]]:
        return [p["rows"].obj for sql, p in self.executed if table in sql]


def _claim(listing_id: int, *, value_text: str | None = None,
           value_jsonb: object | None = None, claim_type: str = "street_name") -> Claim:
    return Claim(
        listing_id=listing_id, source="sreality", source_id_native=str(listing_id),
        claim_type=claim_type, surface="api_json", page_kind="detail",
        extraction_method="portal_structured_field", extractor_id="sr.det.street",
        extractor_version="contract:sreality@1", contract_entry_id=1000,
        snapshot_anchor="unanchored_latest_fetch", first_observed_at=OBSERVED_AT,
        blur_evidence="none", licence_class="portal", history_completeness="full",
        value_text=value_text, value_jsonb=value_jsonb)


def _array_bytes(rows: list[dict]) -> int:
    return sum(len(json.dumps(r, ensure_ascii=False, default=str).encode("utf-8"))
               for r in rows)


# ------------------------------------------------------------------ layer 1: the chunker

def test_a_batch_over_the_byte_budget_is_flushed_in_several_statements(monkeypatch):
    """The regression itself: one array that would exceed the limit becomes N arrays, and
    every row still lands exactly once."""
    monkeypatch.setenv(WRITE_CHUNK_BYTES_ENV, str(64 * 1024))
    monkeypatch.setenv(WRITE_CHUNK_ROWS_ENV, "10000")
    # 200 listings x ~2 KB of value = ~400 KB, ~7 chunks at a 64 KB budget.
    result = IntakeResult(
        claims=[_claim(i, value_text="x" * 2000) for i in range(200)])

    cur = _Cursor()
    inserted, observed, enqueued = write_result(cur, result, batch_id=7)

    arrays = cur.arrays("INSERT INTO location_claims")
    assert len(arrays) > 1
    assert sum(len(a) for a in arrays) == 200
    assert [r["listing_id"] for a in arrays for r in a] == list(range(200))
    # Every chunk fits the budget, and the counters are the SUM over chunks - a chunked
    # write that reported only its last statement would silently under-count the batch row.
    assert all(_array_bytes(a) <= 64 * 1024 for a in arrays)
    assert (inserted, observed, enqueued) == (200, len(arrays), len(arrays))
    assert all(p["batch_id"] == 7 for _, p in cur.executed)


def test_the_row_count_is_the_second_bound(monkeypatch):
    """Bytes are the bound that matters, but a row count keeps a batch of tiny claims from
    becoming one enormous statement anyway."""
    monkeypatch.setenv(WRITE_CHUNK_ROWS_ENV, "50")
    result = IntakeResult(claims=[_claim(i, value_text="Dlouhá") for i in range(200)])

    cur = _Cursor()
    write_result(cur, result, batch_id=1)

    arrays = cur.arrays("INSERT INTO location_claims")
    assert len(arrays) == 4 and all(len(a) == 50 for a in arrays)


def test_a_batch_inside_both_bounds_is_still_one_statement():
    """No behaviour change for the common case — chunking is a ceiling, not a rewrite."""
    result = IntakeResult(claims=[_claim(i, value_text="Dlouhá") for i in range(50)])

    cur = _Cursor()
    write_result(cur, result, batch_id=1)

    assert len(cur.arrays("INSERT INTO location_claims")) == 1


def test_a_chunk_boundary_never_splits_one_listing():
    """THE reviewer's-eye invariant. `claim_fingerprint` is computed in SQL over a tuple
    that starts with (listing_id, source, source_id_native), so two fingerprint-equal claims
    are necessarily one listing's. Keeping a listing whole keeps every fingerprint-equal set
    inside ONE statement, where `DISTINCT ON (claim_fingerprint)` arbitrates it. Split them
    and the second copy would find the first already committed by an earlier statement in
    the same transaction, join the `resighted` cohort, and append an observation row for a
    claim this very batch created."""
    rows = [{"listing_id": i // 4, "n": i} for i in range(40)]

    chunks = list(chunk_rows(rows, max_rows=3, max_bytes=10 ** 9))

    assert sum(len(c) for c in chunks) == 40
    for chunk in chunks:
        assert len(chunk) % 4 == 0  # groups of four, never a partial listing
    seen: set[int] = set()
    for chunk in chunks:
        listing_ids = {r["listing_id"] for r in chunk}
        assert not (listing_ids & seen)  # a listing appears in exactly one chunk
        seen |= listing_ids


def test_a_group_larger_than_the_budget_is_emitted_whole():
    """A budget cannot split an array element, and it must not split a listing either — so
    one over-budget group is emitted as its own chunk rather than silently dropped. Keeping
    that case reachable is why the value cap (layer 2) exists."""
    rows = [{"listing_id": 1, "v": "x" * 5000} for _ in range(3)]

    chunks = list(chunk_rows(rows, max_rows=1, max_bytes=100))

    assert len(chunks) == 1 and len(chunks[0]) == 3


def test_absences_and_enrichment_are_chunked_by_the_same_bounds(monkeypatch):
    """Both share the `jsonb_to_recordset(%(rows)s::jsonb)` pattern, so both share the cap.
    `location_claim_absences` is the table W2's HTML re-mine will write at listing grain."""
    monkeypatch.setenv(WRITE_CHUNK_ROWS_ENV, "25")
    result = IntakeResult(
        absences=[Absence(i, "api_json", "street_name", "not_stated",
                          "portal_structured_field", "d") for i in range(100)],
        enrichment=[EnrichmentTask(i, "portal_structured_field", "sreality_detail_refetch",
                                   "skipped", "ab" * 32) for i in range(100)])

    cur = _Cursor()
    write_result(cur, result, batch_id=1)

    assert len(cur.arrays("location_claim_absences")) == 4
    assert len(cur.arrays("location_enrichment_state")) == 4


def test_the_chunk_bounds_are_env_overridable_and_reject_nonsense(monkeypatch):
    """0 would mean "no bound", which is the state the whole mechanism exists to stop."""
    monkeypatch.setenv(WRITE_CHUNK_ROWS_ENV, "0")
    monkeypatch.setenv(WRITE_CHUNK_BYTES_ENV, "not-a-number")
    result = IntakeResult(claims=[_claim(i, value_text="Dlouhá") for i in range(3)])

    cur = _Cursor()
    write_result(cur, result, batch_id=1)

    assert len(cur.arrays("INSERT INTO location_claims")) == 1
    assert DEFAULT_WRITE_CHUNK_ROWS == 5_000
    assert DEFAULT_WRITE_CHUNK_BYTES == 32 * 1024 * 1024
    # ~8x of headroom under Postgres's 268 435 455-byte ceiling, per statement.
    assert DEFAULT_WRITE_CHUNK_BYTES * 7 <= 268_435_455


# ------------------------------------------------------------------ layer 2: the value cap

def _fat_sreality_payload(padding_bytes: int) -> dict:
    """A post-cutover payload whose `locality.geometry.bounding_box` node carries a blob.

    `bbox_envelope` stores that node VERBATIM into `value_jsonb`, so a reader inherits
    whatever size the portal put there — the same mechanism that once truncated listing
    1588965452's `raw_json` with an 80 KB geometry blob (sreality.yaml §caveats)."""
    payload = json.loads(json.dumps(SREALITY_POST_CUTOVER))
    payload["locality"]["geometry"]["bounding_box"]["encoded"] = "9hETFxX9" * padding_bytes
    return payload


def test_an_oversized_value_is_refused_never_silently_dropped():
    row = listing("sreality", _fat_sreality_payload(4000),
                  listing_id=42, lat=50.0784977, lon=14.4501973)

    result = extract_listing(row, entries_for("sreality"), max_value_bytes=8 * 1024)

    # 1. no claim row for the monster ...
    assert not [c for c in result.claims if c.claim_type == "uncertainty_geometry"]
    # ... and nothing else was collateral damage.
    assert {c.claim_type for c in result.claims} >= {"street_name", "coordinate"}
    assert result.oversized == 1
    # 2. a negative assertion, in migration 382's vocabulary, at the refused claim's grain.
    refusal = [a for a in result.absences if a.field_ == "uncertainty_geometry"]
    assert len(refusal) == 1
    assert refusal[0].reason in ABSENCE_REASONS
    assert refusal[0].reason == "not_attempted"
    assert refusal[0].listing_id == 42 and refusal[0].surface == "api_json"
    assert "bytes" in refusal[0].detail
    # 3. the listing is routed to the refetch cohort, like the truncated-locality path.
    task = [e for e in result.enrichment if e.lane == "sreality_detail_refetch"]
    assert len(task) == 1
    assert task[0].outcome == "error" and task[0].listing_id == 42
    assert task[0].error is not None and "uncertainty_geometry" in task[0].error
    assert len(task[0].input_hash) == 64  # hex; `decode(input_hash,'hex')` on the write


def test_a_value_under_the_cap_is_untouched():
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.0784977, lon=14.4501973)

    result = extract_listing(row, entries_for("sreality"))

    assert [c for c in result.claims if c.claim_type == "uncertainty_geometry"]
    assert result.oversized == 0
    assert not [a for a in result.absences if a.field_ == "uncertainty_geometry"]


def test_the_cap_is_env_overridable(monkeypatch):
    monkeypatch.setenv(MAX_CLAIM_VALUE_BYTES_ENV, "64")
    row = listing("sreality", SREALITY_POST_CUTOVER, lat=50.0784977, lon=14.4501973)

    result = extract_listing(row, entries_for("sreality"))

    assert result.oversized >= 1
    assert DEFAULT_MAX_CLAIM_VALUE_BYTES == 2 * 1024 * 1024


def test_claim_value_bytes_measures_only_the_unbounded_part():
    """Identity and provenance are bounded by the contract; the value is not."""
    small = _claim(1, value_text="Dlouhá")
    big = _claim(1, value_jsonb={"blob": "x" * 5000})

    assert claim_value_bytes(small) == len("Dlouhá".encode("utf-8"))
    assert claim_value_bytes(big) > 5000
    assert claim_value_bytes(_claim(1)) == 0


def test_the_refusal_wins_the_refetch_row_over_the_shape_signal():
    """A legacy-shape sreality row routes to `sreality_detail_refetch` for its SHAPE; an
    oversized value on the SAME listing routes to the same (listing, method, lane). That is
    not a duplicate, it is `ON CONFLICT ... DO UPDATE` "cannot affect row a second time" —
    an aborted run. The refusal is written first so the surviving row is the one whose
    `last_error` says why."""
    payload = json.loads(json.dumps(SREALITY_LEGACY))
    payload["locality"] = dict(SREALITY_POST_CUTOVER["locality"])
    payload["locality"]["geometry"] = json.loads(
        json.dumps(SREALITY_POST_CUTOVER["locality"]["geometry"]))
    payload["locality"]["geometry"]["bounding_box"]["encoded"] = "x" * 40000
    payload["locality"] = {k: v for k, v in payload["locality"].items()
                           if k in ("geometry", "name", "value", "accuracy")}
    payload["locality"].update({"name": "Adresa", "value": "Klatovy", "accuracy": "x"})
    row = listing("sreality", payload, listing_id=9)

    result = extract_listing(row, entries_for("sreality"), max_value_bytes=8 * 1024)

    assert result.oversized == 1
    rows = dedupe_enrichment_rows([e.to_row("claims_intake@2") for e in result.enrichment])
    assert len(rows) == 1
    assert rows[0]["last_outcome"] == "error"
    assert "refused" in rows[0]["last_error"]


def test_enrichment_rows_dedupe_on_the_conflict_target():
    rows = [
        {"listing_id": 1, "method": "portal_structured_field", "lane": "l", "n": "first"},
        {"listing_id": 1, "method": "portal_structured_field", "lane": "l", "n": "second"},
        {"listing_id": 1, "method": "legacy_column", "lane": "l", "n": "other-method"},
        {"listing_id": 2, "method": "portal_structured_field", "lane": "l", "n": "other"},
    ]

    out = dedupe_enrichment_rows(rows)

    assert [r["n"] for r in out] == ["first", "other-method", "other"]


@pytest.mark.parametrize("reason", sorted(ABSENCE_REASONS))
def test_the_absence_vocabulary_is_check_constrained(reason):
    """Pins the fact the refusal design turns on: migration 382 CHECKs `reason` against
    exactly these four, so 'oversized_payload' cannot be written without DDL and the detail
    has to ride on the enrichment row's `last_error` instead (that table has no note
    column)."""
    absence = Absence(1, "api_json", "street_name", reason, "portal_structured_field", "d")

    assert absence.to_row("v")["reason"] in ABSENCE_REASONS
    assert "detail" not in absence.to_row("v")
