"""Rails for the re-master provenance write path (migration 496).

Three concerns, all hermetic (hand-rolled fake connections, no DB):
  1. `invalidate_derived_signals` emits exactly the two producer-predicate
     resets, gates the DINOv3 delete behind BOTH `drop_dinov3` and a
     `to_regclass` probe, and does no SQL at all for empty input.
  2. `mark_image_remastered` writes exactly the provenance SET list and never
     `storage_path` / `download_attempts` (the R2 key is reused, and a
     re-master is not a download attempt), with a PLAIN `phash` assignment.
  3. The NEVER-touch rail: neither function's SQL may name any label store,
     review table or embedding cache the visual-signal program owns.
"""

from __future__ import annotations

import re
from typing import Any

from scraper import db as scraper_db


class _RecordingCursor:
    def __init__(self, probe_result: Any) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rowcount = 3
        self._probe_result = probe_result

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[Any, ...] | None:
        return (self._probe_result,)

    def __enter__(self) -> "_RecordingCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _RecordingConn:
    """Records every statement; `conn.transaction()` is a no-op context."""

    def __init__(self, probe_result: Any = True) -> None:
        self.cursor_obj = _RecordingCursor(probe_result)
        self.transactions = 0

    def cursor(self) -> _RecordingCursor:
        return self.cursor_obj

    def transaction(self) -> "_RecordingConn":
        self.transactions += 1
        return self

    def __enter__(self) -> "_RecordingConn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _sql(conn: _RecordingConn) -> list[str]:
    return [s for s, _p in conn.cursor_obj.executed]


# ---- invalidate_derived_signals --------------------------------------------


def test_invalidate_resets_exactly_the_two_producer_predicates():
    conn = _RecordingConn()
    updated = scraper_db.invalidate_derived_signals(conn, [7, 8])

    assert updated == 3
    assert len(conn.cursor_obj.executed) == 1
    sql, params = conn.cursor_obj.executed[0]
    assert "UPDATE images" in sql
    assert "phash = NULL" in sql
    assert "clip_tagged_at = NULL" in sql
    assert "WHERE id = ANY(%s::bigint[])" in sql
    assert params == ([7, 8],)
    # Nothing but those two columns is reset.
    set_cols = _set_columns(sql)
    assert set_cols == {"phash", "clip_tagged_at"}


def test_invalidate_empty_input_is_a_no_op():
    conn = _RecordingConn()
    assert scraper_db.invalidate_derived_signals(conn, []) == 0
    assert conn.cursor_obj.executed == []
    assert conn.transactions == 0


def test_invalidate_does_not_touch_dinov3_by_default():
    conn = _RecordingConn()
    scraper_db.invalidate_derived_signals(conn, [7])
    joined = " ".join(_sql(conn))
    assert "image_dinov3_embeddings" not in joined
    assert "to_regclass" not in joined


def test_invalidate_drop_dinov3_probes_then_deletes():
    conn = _RecordingConn(probe_result=True)
    scraper_db.invalidate_derived_signals(conn, [7], drop_dinov3=True)

    stmts = _sql(conn)
    assert len(stmts) == 3
    assert "to_regclass('public.image_dinov3_embeddings')" in stmts[1]
    assert "DELETE FROM image_dinov3_embeddings" in stmts[2]
    assert "WHERE image_id = ANY(%s::bigint[])" in stmts[2]
    assert conn.cursor_obj.executed[2][1] == ([7],)


def test_invalidate_drop_dinov3_skips_delete_when_table_absent():
    conn = _RecordingConn(probe_result=None)
    scraper_db.invalidate_derived_signals(conn, [7], drop_dinov3=True)

    stmts = _sql(conn)
    assert len(stmts) == 2  # the UPDATE + the probe, no DELETE
    assert not any("DELETE" in s for s in stmts)


# ---- mark_image_remastered --------------------------------------------------


_SET_COL_RE = re.compile(r"^\s*([a-z_]+)\s*=", re.MULTILINE)


def _set_columns(sql: str) -> set[str]:
    """Column names on the left of `=` inside an UPDATE's SET list."""
    body = sql.split("SET", 1)[1].split("WHERE", 1)[0]
    return set(_SET_COL_RE.findall(body))


def test_mark_image_remastered_set_list_is_exactly_the_provenance_columns():
    conn = _RecordingConn()
    scraper_db.mark_image_remastered(
        conn, 42, rendition="sreality-1800-fit", width=1800, height=1200,
        phash=-99, sreality_url="https://d18-a.sdn.cz/x.jpg",
    )

    # [0] is the invalidation, [1] the stamp.
    sql, params = conn.cursor_obj.executed[1]
    assert _set_columns(sql) == {
        "rendition", "stored_width", "stored_height",
        "phash", "last_download_attempt_at", "sreality_url",
    }
    assert "storage_path" not in sql
    assert "download_attempts" not in sql
    assert params == ("sreality-1800-fit", 1800, 1200, -99,
                      "https://d18-a.sdn.cz/x.jpg", 42)


def test_mark_image_remastered_phash_is_a_plain_assignment():
    """A failed inline hash must leave NULL for the phash lane, never resurrect
    the hash of bytes that no longer exist."""
    sql = scraper_db._MARK_IMAGE_REMASTERED_SQL
    assert "phash = %s" in sql
    assert "COALESCE(%s, phash)" not in sql
    assert "COALESCE(%s, sreality_url)" in sql  # the URL IS preserved


def test_mark_image_remastered_invalidates_first():
    conn = _RecordingConn()
    scraper_db.mark_image_remastered(
        conn, 42, rendition="native", width=None, height=None, phash=None,
    )
    stmts = _sql(conn)
    assert "clip_tagged_at = NULL" in stmts[0]
    assert "rendition = %s" in stmts[1]
    assert conn.cursor_obj.executed[1][1] == ("native", None, None, None, None, 42)


# ---- the NEVER-touch rail ---------------------------------------------------


FORBIDDEN_TABLES = (
    "image_tag_labels",
    "in_training",
    "tag_exam_",
    "tag_review_samples",
    "tag_label_notes",
    "tag_candidates",
    "image_tag_annotations",
    "phash_pair_notes",
    "image_training_examples",
    "image_border_cases",
    "dedup_sim",
    "image_tag_scores",
    "image_room_classifications",
    "listing_image_comparisons",
    "image_clip_tags",
    "image_clip_embeddings",
)


def test_provenance_sql_never_names_a_curation_or_cache_table():
    """The chokepoint re-arms two producers; it is not a cascade delete. The CLIP
    tables in particular are upserted in place and joined for tag centroids."""
    corpus = " ".join((
        scraper_db._INVALIDATE_DERIVED_SIGNALS_SQL,
        scraper_db._DINOV3_TABLE_PROBE_SQL,
        scraper_db._DROP_DINOV3_EMBEDDINGS_SQL,
        scraper_db._MARK_IMAGE_REMASTERED_SQL,
    ))
    for name in FORBIDDEN_TABLES:
        assert name not in corpus, f"{name} must never be touched here"


# ---- mark_image_stored backwards-compat -------------------------------------


_PRE_496_SQL = """
            UPDATE images
            SET storage_path = %s,
                phash = COALESCE(%s, phash),
                last_download_attempt_at = now(),
                download_attempts = download_attempts + 1
            WHERE id = %s
            """


def test_mark_image_stored_without_provenance_is_byte_identical():
    conn = _RecordingConn()
    scraper_db.mark_image_stored(conn, 42, "sreality/1/2.jpg", 123)
    sql, params = conn.cursor_obj.executed[0]
    assert sql == _PRE_496_SQL
    assert params == ("sreality/1/2.jpg", 123, 42)


def test_mark_image_stored_writes_provenance_only_when_measured():
    conn = _RecordingConn()
    scraper_db.mark_image_stored(
        conn, 42, "sreality/1/2.jpg", 123,
        rendition="sreality-1800-fit", width=1800,
    )
    sql, params = conn.cursor_obj.executed[0]
    assert _set_columns(sql) == {
        "storage_path", "phash", "rendition", "stored_width",
        "last_download_attempt_at", "download_attempts",
    }
    assert "stored_height" not in sql  # not measured -> not written
    assert params == ("sreality/1/2.jpg", 123, "sreality-1800-fit", 1800, 42)
