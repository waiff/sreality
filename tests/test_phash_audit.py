"""api.property_dedup.phash_audit: the /phash-audit range browse over dedup_pair_audit
pairs, chunked-scan pagination. Hermetic fake conn — no DB."""

from __future__ import annotations

from typing import Any

import api.property_dedup as dedup


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if "image_training_examples te ON te.image_id = i.id" in s:
            self._rows = [(sid,) for sid in self._conn.trained_sreality_ids]
        elif "count(*) FROM dedup_pair_audit" in s:
            self._rows = [(self._conn.scanned,)]
        elif "WITH scoped AS" in s:
            idx = self._conn.chunk_calls
            self._conn.chunk_calls += 1
            if self._conn.chunk_responses is not None:
                self._rows = list(self._conn.chunk_responses[idx]) if idx < len(self._conn.chunk_responses) else []
            else:
                self._rows = list(self._conn.join_rows)
        else:
            self._rows = []

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(
        self, *, scanned: int = 0, join_rows=None,
        chunk_responses: list[list[tuple[Any, ...]]] | None = None,
        trained_sreality_ids: list[int] | None = None,
    ) -> None:
        self.scanned = scanned
        self.join_rows = join_rows or []
        self.chunk_responses = chunk_responses
        self.trained_sreality_ids = trained_sreality_ids or []
        self.chunk_calls = 0
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    def chunk_queries(self) -> list[tuple[str, Any]]:
        return [(s, p) for s, p in self.executed if "WITH scoped AS" in s]


def _join_row(detail: dict | None = None) -> tuple[Any, ...]:
    # 25 columns matching phash_audit's join SELECT.
    d = detail if detail is not None else {
        "stage": "visual", "reason": "visual_different", "verdict": "Low",
        "cosine": 0.86, "room_type": "kitchen", "phash_pairs": 0,
        "phash_min_pairs": 2, "phash_threshold": 6,
    }
    return (
        99, -5, 42, 10, 11, "merged", "byt", "2026-07-01T00:00:00Z", "visual", d,
        1001, "https://x/a.jpg", None, "kitchen", "kitchen", 0.91, None,
        2002, "https://x/b.jpg", None, "kitchen", "kitchen", 0.88, None,
        9,
    )


def test_hamming_range_passed_through_and_result_shaped() -> None:
    conn = _FakeConn(scanned=3, join_rows=[_join_row()])
    out = dedup.phash_audit(conn, hamming_min=7, hamming_max=15)
    join_sql, join_params = conn.chunk_queries()[0]
    assert join_params["hmin"] == 7 and join_params["hmax"] == 15
    assert "BETWEEN %(hmin)s AND %(hmax)s" in join_sql
    row = out["data"][0]
    assert row["audit_id"] == 99
    assert row["hamming"] == 9
    assert row["stage"] == "visual"
    assert row["left_property_id"] == 10 and row["right_property_id"] == 11
    assert row["left_image"] == {
        "image_id": 1001, "sreality_url": "https://x/a.jpg",
        "storage_path": None, "room_type": "kitchen",
        "fine_tag": "kitchen", "confidence": 0.91, "render_score": None,
    }
    assert out["scanned_pairs"] == 3
    assert out["returned"] == 1


def test_audit_breakdown_computed_from_detail_so_the_true_decider_is_visible() -> None:
    # The whole point: phash found nothing (phash_pairs=0) but the row still shows up
    # because ANOTHER stage (visual) decided the pair — the breakdown must say so, not
    # imply phash drove it just because this page is about Hamming distance.
    conn = _FakeConn(scanned=1, join_rows=[_join_row()])
    out = dedup.phash_audit(conn, hamming_min=7, hamming_max=15)
    keys = [r["key"] for r in out["data"][0]["audit_breakdown"]]
    assert "verdict" in keys
    verdict_rung = next(r for r in out["data"][0]["audit_breakdown"] if r["key"] == "verdict")
    assert verdict_rung["value"] == "Low"


def test_category_main_and_outcome_scope_both_the_count_and_chunk_queries() -> None:
    conn = _FakeConn(scanned=0, join_rows=[])
    dedup.phash_audit(
        conn, hamming_min=0, hamming_max=15, category_main="dum", outcome="dismissed",
    )
    for s, params in conn.executed:
        assert "a.category_main = %(category_main)s" in s
        assert "a.outcome = %(outcome)s" in s
        assert params["category_main"] == "dum"
        assert params["outcome"] == "dismissed"


def test_no_scope_filters_omit_the_where_clause() -> None:
    conn = _FakeConn(scanned=0, join_rows=[])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15)
    for s, params in conn.executed:
        assert "category_main" not in (params or {})
        assert "outcome" not in (params or {})


def test_room_types_requires_both_sides_to_match_the_same_tag_from_the_set() -> None:
    # Not "either side happens to carry ANY of these tags" — a chodba<->kuchyne pair
    # passing a kuchyne filter is exactly the confusion this must not reproduce (the
    # engine's own phash pass is room-blind by design, but the Tag filter here means
    # "show me same-room pairs, for one of these rooms").
    conn = _FakeConn(scanned=1, join_rows=[])
    dedup.phash_audit(
        conn, hamming_min=0, hamming_max=15, room_types=["kitchen", "bathroom"],
    )
    join_sql, join_params = conn.chunk_queries()[0]
    assert "ta.logical_tag = ANY(%(room_types)s)" in join_sql
    assert "tb.logical_tag = ANY(%(room_types)s)" in join_sql
    assert "ta.logical_tag = tb.logical_tag" in join_sql
    assert join_params["room_types"] == ["kitchen", "bathroom"]


def test_no_room_types_omits_the_tag_clause() -> None:
    conn = _FakeConn(scanned=1, join_rows=[])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, room_types=None)
    join_sql, join_params = conn.chunk_queries()[0]
    assert "ta.logical_tag = ANY" not in join_sql
    assert "room_types" not in join_params


def test_first_chunk_starts_at_scan_offset_sized_to_remaining_ceiling() -> None:
    conn = _FakeConn(scanned=200, join_rows=[_join_row()])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, scan_offset=0)
    _, join_params = conn.chunk_queries()[0]
    assert join_params["off"] == 0
    # scanned_pairs=200 < the 800 chunk size -> the chunk shrinks to what's left, not 800.
    assert join_params["chunk"] == 200


def test_scan_offset_resumes_from_the_given_cursor() -> None:
    conn = _FakeConn(scanned=2000, join_rows=[_join_row()])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, scan_offset=800)
    _, join_params = conn.chunk_queries()[0]
    assert join_params["off"] == 800
    assert join_params["chunk"] == 800  # 2000-800=1200 remaining, capped at the 800 chunk


def test_one_call_processes_exactly_one_chunk() -> None:
    # Predictable, bounded per-call latency (verified live: ~5-7s/chunk regardless of
    # filter) — no internal multi-chunk loop. The CALLER (the page) is what keeps
    # asking for more via next_scan_offset until a full page or true exhaustion.
    conn = _FakeConn(scanned=2000, chunk_responses=[[_join_row()], [_join_row()]])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, limit=100)
    assert len(conn.chunk_queries()) == 1
    assert out["returned"] == 1
    assert out["scanned_so_far"] == 800
    assert out["next_scan_offset"] == 800


def test_a_sparse_chunk_can_legitimately_return_fewer_rows_than_limit() -> None:
    # Zero matches in this chunk does NOT mean "done" — next_scan_offset must still
    # point past it so the caller's loop keeps going.
    conn = _FakeConn(scanned=3800, chunk_responses=[[]])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, limit=100)
    assert out["returned"] == 0
    assert out["scanned_so_far"] == 800
    assert out["next_scan_offset"] == 800


def test_next_scan_offset_is_null_once_the_ceiling_or_population_is_exhausted() -> None:
    conn = _FakeConn(scanned=50, join_rows=[_join_row()])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, scan_offset=0)
    # scanned_pairs=50 all fit in one chunk -> nothing left to scan.
    assert out["next_scan_offset"] is None
    assert out["scanned_so_far"] == 50


def test_ceiling_bounds_the_chunk_regardless_of_true_population_size() -> None:
    conn = _FakeConn(scanned=50_000, chunk_responses=[[]])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, scan_offset=3200)
    # Only 600 pairs remain before the 3800 ceiling — the chunk must shrink to that,
    # not request 800, and next_scan_offset must reflect the ceiling, not the true
    # 50,000-row population.
    _, first_params = conn.chunk_queries()[0]
    assert first_params["chunk"] == 600
    assert out["scanned_so_far"] == 3800
    assert out["next_scan_offset"] is None
    assert out["scan_cap"] == 3800


def test_scan_offset_already_at_or_past_the_ceiling_skips_the_chunk_query() -> None:
    conn = _FakeConn(scanned=50_000)
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, scan_offset=3800)
    assert len(conn.chunk_queries()) == 0
    assert out["returned"] == 0
    assert out["next_scan_offset"] is None
    assert out["next_scan_offset"] is None
    assert out["scan_cap"] == 3800


def test_training_only_with_no_trained_images_short_circuits() -> None:
    # An empty training set means zero possible matches — skip the count/chunk
    # queries entirely rather than scanning for something that can't exist.
    conn = _FakeConn(scanned=2000, trained_sreality_ids=[])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_only=True)
    assert out == {
        "data": [], "returned": 0, "scanned_pairs": 0,
        "scan_cap": 3800, "scanned_so_far": 0, "next_scan_offset": None,
    }
    assert len(conn.chunk_queries()) == 0
    assert not any("count(*) FROM dedup_pair_audit" in s for s, _ in conn.executed)


def test_training_only_narrows_scope_to_trained_listings_and_chunk_to_exact_images() -> None:
    conn = _FakeConn(scanned=1, trained_sreality_ids=[111, 222], join_rows=[_join_row()])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_only=True)
    scope_sql, scope_params = next(
        (s, p) for s, p in conn.executed if "count(*) FROM dedup_pair_audit" in s
    )
    assert "a.left_sreality_id = ANY(%(trained_sreality_ids)s)" in scope_sql
    assert "a.right_sreality_id = ANY(%(trained_sreality_ids)s)" in scope_sql
    assert scope_params["trained_sreality_ids"] == [111, 222]
    join_sql, _ = conn.chunk_queries()[0]
    assert "image_training_examples te WHERE te.image_id = ia.id" in join_sql
    assert "image_training_examples te WHERE te.image_id = ib.id" in join_sql


def test_training_only_false_never_queries_trained_images_or_adds_the_clause() -> None:
    conn = _FakeConn(scanned=1, join_rows=[_join_row()])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_only=False)
    assert not any(
        "image_training_examples te ON te.image_id = i.id" in s for s, _ in conn.executed
    )
    join_sql, _ = conn.chunk_queries()[0]
    assert "image_training_examples" not in join_sql


def test_training_label_narrows_lookup_and_chunk_clause_to_that_specific_label() -> None:
    conn = _FakeConn(scanned=1, trained_sreality_ids=[111], join_rows=[_join_row()])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_label="kuchyně")
    lookup_sql, lookup_params = conn.executed[0]
    assert "image_training_examples te ON te.image_id = i.id" in lookup_sql
    assert "te.label = %(training_label)s" in lookup_sql
    assert lookup_params["training_label"] == "kuchyně"
    join_sql, join_params = conn.chunk_queries()[0]
    # Both EXISTS arms (left image, right image) must carry the label match —
    # not just "any label at all" once a specific one is requested.
    assert join_sql.count("te.label = %(training_label)s") == 2
    assert join_params["training_label"] == "kuchyně"


def test_training_label_implies_training_scoping_even_if_training_only_is_false() -> None:
    # A specific label request is unambiguous intent — don't require the caller to
    # also pass training_only=True redundantly.
    conn = _FakeConn(scanned=1, trained_sreality_ids=[111], join_rows=[_join_row()])
    dedup.phash_audit(
        conn, hamming_min=0, hamming_max=15, training_only=False, training_label="kuchyně",
    )
    assert len(conn.chunk_queries()) == 1
    join_sql, _ = conn.chunk_queries()[0]
    assert "image_training_examples" in join_sql


def test_training_label_with_no_matching_images_short_circuits() -> None:
    conn = _FakeConn(scanned=2000, trained_sreality_ids=[])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_label="vzácný tag")
    assert out["returned"] == 0
    assert out["scanned_pairs"] == 0
    assert len(conn.chunk_queries()) == 0


def test_training_exclude_adds_a_negative_scope_clause_and_no_post_join_clause() -> None:
    conn = _FakeConn(scanned=1, trained_sreality_ids=[111, 222], join_rows=[_join_row()])
    dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_exclude=True)
    scope_sql, scope_params = next(
        (s, p) for s, p in conn.executed if "count(*) FROM dedup_pair_audit" in s
    )
    assert "NOT (a.left_sreality_id = ANY(%(trained_sreality_ids)s)" in scope_sql
    assert scope_params["trained_sreality_ids"] == [111, 222]
    # The scope-level NOT already guarantees neither image is trained (a listing
    # absent from trained_sreality_ids can't own a trained image) — no post-join
    # re-check needed, unlike the inclusion case.
    join_sql, _ = conn.chunk_queries()[0]
    assert "image_training_examples" not in join_sql


def test_training_exclude_with_an_empty_training_set_excludes_nothing() -> None:
    # Nothing trained yet -> nothing to exclude -> behaves like no filter at all,
    # not a short-circuit to zero (that's the inclusion case's behavior, not this one).
    conn = _FakeConn(scanned=500, trained_sreality_ids=[], join_rows=[_join_row()])
    out = dedup.phash_audit(conn, hamming_min=0, hamming_max=15, training_exclude=True)
    scope_sql, scope_params = next(
        (s, p) for s, p in conn.executed if "count(*) FROM dedup_pair_audit" in s
    )
    assert "trained_sreality_ids" not in scope_sql
    assert "trained_sreality_ids" not in scope_params
    assert out["scanned_pairs"] == 500
    assert len(conn.chunk_queries()) == 1


def test_training_exclude_takes_priority_over_training_only_and_label() -> None:
    conn = _FakeConn(scanned=1, trained_sreality_ids=[111], join_rows=[_join_row()])
    dedup.phash_audit(
        conn, hamming_min=0, hamming_max=15,
        training_only=True, training_label="kuchyně", training_exclude=True,
    )
    scope_sql, _ = next(
        (s, p) for s, p in conn.executed if "count(*) FROM dedup_pair_audit" in s
    )
    assert "NOT (a.left_sreality_id" in scope_sql
    assert "te.label = %(training_label)s" not in scope_sql
    join_sql, _ = conn.chunk_queries()[0]
    assert "image_training_examples" not in join_sql
