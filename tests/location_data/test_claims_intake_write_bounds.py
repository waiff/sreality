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
    Entry,
    DEFAULT_WRITE_CHUNK_BYTES,
    DEFAULT_WRITE_CHUNK_ROWS,
    MAX_CLAIM_VALUE_BYTES_ENV,
    WRITE_CHUNK_BYTES_ENV,
    WRITE_CHUNK_ROWS_ENV,
    Claim,
    IntakeResult,
    chunk_rows,
    claim_value_bytes,
    extract_listing,
    write_result,
)
from tests.location_data.claim_intake_fixtures import (
    OBSERVED_AT,
    SREALITY_LEGACY,
    SREALITY_POST_CUTOVER,
    listing,
)



class _Cursor:
    """Records what was executed. `fetchone` answers the claim write's two counters."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: dict | None = None) -> None:
        self.executed.append((" ".join(sql.split()), params or {}))

    def fetchone(self) -> tuple[int, int]:
        rows = self.executed[-1][1]["rows"].obj
        return (len(rows), 1)

    def arrays(self, table: str) -> list[list[dict]]:
        return [p["rows"].obj for sql, p in self.executed if table in sql]


# The fields below that migration 498 dropped from the TABLE (source_id_native, page_kind,
# extractor_id, extractor_version, snapshot_anchor, history_completeness) are still spelled
# here on purpose: the readers compute them and the fingerprint still hashes them.
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
    inserted, enqueued = write_result(cur, result)

    arrays = cur.arrays("INSERT INTO location_claims")
    assert len(arrays) > 1
    assert sum(len(a) for a in arrays) == 200
    assert [r["listing_id"] for a in arrays for r in a] == list(range(200))
    # Every chunk fits the budget, and the counters are the SUM over chunks - a chunked
    # write that reported only its last statement would silently under-count the batch row.
    assert all(_array_bytes(a) <= 64 * 1024 for a in arrays)
    assert (inserted, enqueued) == (200, len(arrays))
    # `rows` is the ONLY parameter now: W1-b dropped `location_claims.batch_id`, so the
    # claim is no longer a child of the run ledger (`location_claim_batches` stays — it is
    # the lane's cursor).
    assert all(set(p) == {"rows"} for _, p in cur.executed)


def test_the_row_count_is_the_second_bound(monkeypatch):
    """Bytes are the bound that matters, but a row count keeps a batch of tiny claims from
    becoming one enormous statement anyway."""
    monkeypatch.setenv(WRITE_CHUNK_ROWS_ENV, "50")
    result = IntakeResult(claims=[_claim(i, value_text="Dlouhá") for i in range(200)])

    cur = _Cursor()
    write_result(cur, result)

    arrays = cur.arrays("INSERT INTO location_claims")
    assert len(arrays) == 4 and all(len(a) == 50 for a in arrays)


def test_a_batch_inside_both_bounds_is_still_one_statement():
    """No behaviour change for the common case — chunking is a ceiling, not a rewrite."""
    result = IntakeResult(claims=[_claim(i, value_text="Dlouhá") for i in range(50)])

    cur = _Cursor()
    write_result(cur, result)

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


def test_the_chunk_bounds_are_env_overridable_and_reject_nonsense(monkeypatch):
    """0 would mean "no bound", which is the state the whole mechanism exists to stop."""
    monkeypatch.setenv(WRITE_CHUNK_ROWS_ENV, "0")
    monkeypatch.setenv(WRITE_CHUNK_BYTES_ENV, "not-a-number")
    result = IntakeResult(claims=[_claim(i, value_text="Dlouhá") for i in range(3)])

    cur = _Cursor()
    write_result(cur, result)

    assert len(cur.arrays("INSERT INTO location_claims")) == 1
    assert DEFAULT_WRITE_CHUNK_ROWS == 5_000
    assert DEFAULT_WRITE_CHUNK_BYTES == 32 * 1024 * 1024
    # ~8x of headroom under Postgres's 268 435 455-byte ceiling, per statement.
    assert DEFAULT_WRITE_CHUNK_BYTES * 7 <= 268_435_455


# ------------------------------------------------------------------ layer 2: the value cap

# The cap is a property of the LANE, not of any portal's contract. It used to be exercised
# through sreality's `sr.det.geometry` entry, whose `bbox_envelope` reader stored a portal
# node verbatim into `value_jsonb` — a real oversized value, and a dependency on one
# contract entry surviving every future rewrite of that contract. It did not (W1-c). The
# synthetic entry below states the same thing the shipped one did — a reader that copies a
# portal value into a claim — and states it in this file, where the rail lives.
_FAT_ENTRY = Entry(
    id=9001, source="sreality", contract_id=1, contract_version=1,
    entry_id="sr.det.fat", surface="api_json", page_kind="detail",
    locator={"reader": "scalar", "json_pointer": "/blob"}, claim_type="street_name",
    extraction_method="portal_structured_field", subject_scope={}, transform=(),
    precision_map={}, default_blur_evidence="none", default_licence_class="portal",
    guards=())


def _fat_payload(padding_bytes: int, base: dict | None = None) -> dict:
    """A payload carrying one value too large to be a location claim — the shape of the
    80 KB geometry blob that truncated listing 1588965452's `raw_json` (sreality.yaml
    §caveats), which is the incident this cap exists for."""
    payload = json.loads(json.dumps(base if base is not None else SREALITY_POST_CUTOVER))
    payload["blob"] = "9hETFxX9" * padding_bytes
    return payload


def test_an_oversized_value_is_refused_never_silently_dropped():
    row = listing("sreality", _fat_payload(4000), listing_id=42)

    result = extract_listing(row, [_FAT_ENTRY], max_value_bytes=8 * 1024)

    # 1. no claim row for the monster ...
    assert result.claims == []
    # 2. counted under its own reason, at the refused claim's grain — and logged. A
    # counter, not a row: `location_claim_absences` was written by every lane and read by
    # none (rule 25) and is gone (migration 498), so what survives is the tally the
    # operator actually reads.
    assert result.refusals["oversized_value:street_name"] == 1


def test_a_value_under_the_cap_is_untouched():
    row = listing("sreality", _fat_payload(4), listing_id=42)

    result = extract_listing(row, [_FAT_ENTRY])

    assert [c.claim_type for c in result.claims] == ["street_name"]
    assert not [r for r in result.refusals if r.startswith("oversized_value")]


def test_the_cap_is_env_overridable(monkeypatch):
    monkeypatch.setenv(MAX_CLAIM_VALUE_BYTES_ENV, "64")
    row = listing("sreality", _fat_payload(100), listing_id=42)

    result = extract_listing(row, [_FAT_ENTRY])

    assert result.refusals["oversized_value:street_name"] == 1
    assert DEFAULT_MAX_CLAIM_VALUE_BYTES == 2 * 1024 * 1024


def test_claim_value_bytes_measures_only_the_unbounded_part():
    """Identity and provenance are bounded by the contract; the value is not."""
    small = _claim(1, value_text="Dlouhá")
    big = _claim(1, value_jsonb={"blob": "x" * 5000})

    assert claim_value_bytes(small) == len("Dlouhá".encode("utf-8"))
    assert claim_value_bytes(big) > 5000
    assert claim_value_bytes(_claim(1)) == 0


def test_a_legacy_shape_row_counts_both_refusals_separately():
    """A legacy-shape sreality row with an oversized value used to collide on ONE
    `location_enrichment_state` primary key — `ON CONFLICT … DO UPDATE` "cannot affect row a
    second time", i.e. an aborted run. Two counters cannot collide (and that table is gone
    as of migration 498)."""
    row = listing("sreality", _fat_payload(5000, base=SREALITY_LEGACY), listing_id=9)

    result = extract_listing(row, [_FAT_ENTRY], max_value_bytes=8 * 1024)

    assert result.refusals["oversized_value:street_name"] == 1
    assert result.refusals["sreality_payload_shape:legacy"] == 1
