"""scripts/dinov3_embed_backfill.py — the checkpoint/resume anti-join, the six-fact
key, the write-rate throttle and the dry-run readout.

The load-bearing claim under test is that the TARGET TABLE is the checkpoint: pending =
a stored image with no row under this EXACT six-fact identity, so an image embedded
under a different revision / resolution / preprocessing / dtype is still pending here.
That is what makes a re-run a no-op, a dead pod cost minutes, and two encoder
configurations two populations instead of one corrupted one
(docs/design/new-dedup/ENCODER-DECISION.md §4.1, §5.5).

Hermetic: a fake connection that evaluates the anti-join's semantics in Python after
asserting the real SQL binds all six facts. No Postgres, no R2, no torch, no HF hub.
"""

from __future__ import annotations

import sys

import pytest

from scraper.dinov3_config import IDENTITY_FIELDS
from scripts import dinov3_embed_backfill as bf

IDENTITY = {
    "model": "facebook/dinov3-vitb16-pretrain-lvd1689m",
    "revision": "a" * 40,
    "library": "transformers",
    "pooling": "cls",
    "resolution": 224,
    "preprocessing": "letterbox_pad",
    "dtype": "bf16",
}


def _emb(image_id: int, **overrides) -> dict:
    return {"image_id": image_id, **IDENTITY, **overrides}


# --- the fake connection ------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql: str, params=None):
        norm = " ".join(sql.split())
        self._conn.executed.append((norm, params))
        if norm.upper().startswith("SET LOCAL"):
            self._rows = []
        elif norm.startswith("SELECT count(*) FROM images WHERE storage_path"):
            self._rows = [(sum(1 for _i, path in self._conn.images if path),)]
        elif norm.startswith("SELECT count(*) FROM image_dinov3_embeddings"):
            self._rows = [(len(self._conn.matching_embeddings(norm, params)),)]
        elif norm.startswith("SELECT count(*) FROM images i"):
            self._rows = [(len(self._conn.pending(norm, params)),)]
        elif norm.startswith("SELECT i.id, i.storage_path"):
            self._rows = self._conn.pending(norm, params)
        else:  # pragma: no cover - an unrecognised statement is a test bug, not a pass
            raise AssertionError(f"fake conn saw unexpected SQL: {norm[:120]}")

    def executemany(self, sql: str, seq):
        self._conn.executed.append((" ".join(sql.split()), None))
        self._conn.written.extend(list(seq))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Transaction:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _FakeConn:
    """Evaluates the anti-join's SEMANTICS, after asserting the real SQL text binds
    every one of the six identity facts — so the fake cannot drift into agreeing with
    a query that silently dropped one."""

    def __init__(self, images: list[tuple[int, str | None]], embeddings: list[dict]) -> None:
        self.images = images
        self.embeddings = embeddings
        self.executed: list[tuple[str, dict | None]] = []
        self.written: list[tuple] = []

    def cursor(self):
        return _FakeCursor(self)

    def transaction(self):
        return _Transaction()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    @staticmethod
    def _assert_binds_all_six(sql: str, params: dict, prefix: str) -> None:
        for field in IDENTITY_FIELDS:
            assert f"{prefix}.{field} = %({field})s" in sql, (
                f"the query does not compare {field} — the six-fact key is not enforced"
            )
            assert field in params, f"the caller did not bind {field}"

    def matching_embeddings(self, sql: str, params: dict) -> list[dict]:
        self._assert_binds_all_six(sql, params, "e")
        return [
            e for e in self.embeddings
            if all(e[f] == params[f] for f in IDENTITY_FIELDS)
        ]

    def pending(self, sql: str, params: dict) -> list[tuple[int, str]]:
        self._assert_binds_all_six(sql, params, "e")
        embedded = {e["image_id"] for e in self.matching_embeddings(sql, params)}
        shards = params.get("shards", 1)
        after_id = params.get("after_id", 0)
        rows = [
            (image_id, path)
            for image_id, path in sorted(self.images)
            if path is not None
            and image_id > after_id
            and (shards == 1 or image_id % shards == params["shard"])
            and image_id not in embedded
        ]
        batch = params.get("batch")
        return rows[:batch] if batch else rows


def _pending(conn, **kwargs) -> list[int]:
    args = {"identity": IDENTITY, "batch": 100, "shard": 0, "shards": 1, "after_id": 0}
    args.update(kwargs)
    return [row[0] for row in bf.select_pending(conn, **args)]


# --- the checkpoint -----------------------------------------------------------


def test_an_image_already_embedded_under_this_config_is_not_pending():
    conn = _FakeConn(
        images=[(1, "img/1.jpg"), (2, "img/2.jpg"), (3, "img/3.jpg")],
        embeddings=[_emb(2)],
    )
    assert _pending(conn) == [1, 3]


@pytest.mark.parametrize(
    "differing",
    [
        {"model": "facebook/dinov3-vitl16-pretrain-lvd1689m"},
        {"revision": "b" * 40},
        {"library": "timm"},
        {"pooling": "mean"},
        {"resolution": 512},
        {"preprocessing": "square_squash"},
        {"dtype": "fp32"},
    ],
)
def test_an_image_embedded_under_a_different_config_is_still_pending(differing):
    # The whole point of the six-fact key: any one of them differing is a DIFFERENT
    # POPULATION, so this config still owes that image a vector.
    conn = _FakeConn(
        images=[(1, "img/1.jpg"), (2, "img/2.jpg")],
        embeddings=[_emb(2, **differing)],
    )
    assert _pending(conn) == [1, 2]


def test_a_row_under_both_configs_is_pending_under_neither():
    conn = _FakeConn(
        images=[(1, "img/1.jpg"), (2, "img/2.jpg")],
        embeddings=[_emb(2), _emb(2, resolution=512)],
    )
    assert _pending(conn) == [1]


def test_images_without_stored_bytes_are_never_pending():
    conn = _FakeConn(images=[(1, None), (2, "img/2.jpg")], embeddings=[])
    assert _pending(conn) == [2]


def test_sharding_partitions_the_corpus():
    images = [(i, f"img/{i}.jpg") for i in range(1, 9)]
    conn = _FakeConn(images=images, embeddings=[])
    assert _pending(conn, shard=0, shards=4) == [4, 8]
    assert _pending(conn, shard=1, shards=4) == [1, 5]


def test_the_in_run_cursor_moves_past_a_chunk_that_wrote_nothing():
    # A chunk whose downloads all failed writes no rows, so the anti-join alone would
    # hand back the same ids forever. The cursor is what stops that wedging a run —
    # and it resets to 0 next run, so the failure is retried rather than skipped.
    conn = _FakeConn(images=[(1, "a"), (2, "b"), (3, "c")], embeddings=[])
    assert _pending(conn, batch=2) == [1, 2]
    assert _pending(conn, batch=2, after_id=2) == [3]


def test_the_batch_limit_is_applied():
    conn = _FakeConn(images=[(i, f"img/{i}.jpg") for i in range(1, 21)], embeddings=[])
    assert _pending(conn, batch=5) == [1, 2, 3, 4, 5]


# --- the SQL itself -----------------------------------------------------------


def test_pending_sql_anti_joins_on_all_seven_key_columns():
    sql = " ".join(bf._PENDING_SQL.split())
    assert "NOT EXISTS" in sql
    assert "e.image_id = i.id" in sql
    for field in IDENTITY_FIELDS:
        assert f"e.{field} = %({field})s" in sql


def test_insert_conflicts_on_the_whole_six_fact_key_and_does_nothing():
    sql = " ".join(bf._INSERT_SQL.split())
    assert (
        "ON CONFLICT (image_id, model, revision, library, pooling, resolution, "
        "preprocessing, dtype) DO NOTHING" in sql
    )
    # DO UPDATE would silently overwrite a byte-identical recomputation, and worse,
    # would let one (image, model) row mean two different encoder configurations.
    assert "DO UPDATE" not in sql


def test_insert_writes_the_vector_as_a_halfvec():
    assert "%s::halfvec" in bf._INSERT_SQL


# --- the write-rate throttle ---------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# A rate of exactly 1,552 bytes/second, i.e. one row per second.
ONE_ROW_PER_SECOND_MB_H = bf.HALFVEC_ROW_BYTES * 3600 / (1024 * 1024)


def test_throttle_sleeps_off_the_unspent_byte_budget():
    sleeper = _Recorder()
    throttle = bf.WriteThrottle(ONE_ROW_PER_SECOND_MB_H, sleep=sleeper)
    assert throttle.pace(1000, elapsed_s=100.0) == pytest.approx(900.0)
    assert sleeper.calls == [pytest.approx(900.0)]
    assert throttle.slept_s == pytest.approx(900.0)


def test_throttle_does_not_sleep_when_the_batch_was_already_slower_than_the_ceiling():
    sleeper = _Recorder()
    throttle = bf.WriteThrottle(ONE_ROW_PER_SECOND_MB_H, sleep=sleeper)
    assert throttle.pace(10, elapsed_s=60.0) == 0.0
    assert sleeper.calls == []


def test_throttle_budget_is_rows_times_row_bytes_over_the_rate():
    throttle = bf.WriteThrottle(500, sleep=_Recorder())
    expected = (256 * bf.HALFVEC_ROW_BYTES) / (500 * 1024 * 1024 / 3600)
    assert throttle.budget_s(256) == pytest.approx(expected)


def test_throttle_writes_nothing_means_no_sleep():
    sleeper = _Recorder()
    bf.WriteThrottle(1, sleep=sleeper).pace(0, elapsed_s=0.0)
    assert sleeper.calls == []


@pytest.mark.parametrize("bad", [0, -1, -0.5])
def test_throttle_refuses_a_nonsense_rate(bad):
    with pytest.raises(ValueError):
        bf.WriteThrottle(bad)


def test_the_write_ceiling_flag_is_required_and_has_no_default(monkeypatch):
    # Required BECAUSE the safe value depends on the Supabase dashboard's live disk
    # utilisation: 90% auto-expands, 95% with the quota gone puts the WHOLE project
    # in read-only (ENCODER-DECISION §5.0/§5.5).
    monkeypatch.setattr(sys, "argv", ["dinov3_embed_backfill", "--dry-run"])
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")
    with pytest.raises(SystemExit) as exc:
        bf.main()
    assert exc.value.code == 2


# --- the dry run ---------------------------------------------------------------


def _run(monkeypatch, conn, argv: list[str]) -> int:
    import psycopg

    monkeypatch.setattr(sys, "argv", ["dinov3_embed_backfill", *argv])
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")
    monkeypatch.setattr(bf, "encoder_identity", lambda *a, **k: dict(IDENTITY))
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: conn)
    return bf.main()


def test_dry_run_reports_and_writes_nothing(monkeypatch, caplog):
    conn = _FakeConn(
        images=[(i, f"img/{i}.jpg") for i in range(1, 11)],
        embeddings=[_emb(1), _emb(2)],
    )
    monkeypatch.setattr(
        bf.image_storage, "is_configured",
        lambda: (_ for _ in ()).throw(AssertionError("dry run must not touch R2")),
    )
    with caplog.at_level("INFO"):
        assert _run(monkeypatch, conn, ["--max-write-mb-per-hour", "500", "--dry-run"]) == 0
    assert conn.written == []
    text = caplog.text
    assert "pending=8" in text
    assert "embedded=2/10" in text
    assert "letterbox_pad" in text  # the resolved identity is echoed


def test_missing_db_url_is_a_hard_error(monkeypatch):
    monkeypatch.setattr(sys, "argv",
                        ["dinov3_embed_backfill", "--max-write-mb-per-hour", "1", "--dry-run"])
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    assert bf.main() == 2


def test_an_under_specified_encoder_is_refused_before_any_db_work(monkeypatch):
    # The shipped config is provisional on purpose; main() must raise the rail rather
    # than embed against a guessed resolution/dtype.
    import psycopg

    monkeypatch.setattr(sys, "argv",
                        ["dinov3_embed_backfill", "--max-write-mb-per-hour", "1", "--dry-run"])
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://fake")
    monkeypatch.setattr(
        psycopg, "connect",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not reach the DB")),
    )
    with pytest.raises(RuntimeError) as exc:
        bf.main()
    assert "ENCODER-DECISION" in str(exc.value)
