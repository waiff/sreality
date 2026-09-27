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

import json
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
        if "NOT EXISTS" in sql:
            self._assert_binds_all_six(sql, params, "e")
            embedded = {e["image_id"] for e in self.matching_embeddings(sql, params)}
        else:   # R2 mode's scope query: no anti-join, the manifest is the checkpoint
            embedded = set()
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


# A rate of exactly one vector row's bytes a second, i.e. one image per second.
ONE_ROW_PER_SECOND_MB_H = bf.VECTOR_ROW_BYTES * 3600 / (1024 * 1024)


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


def test_throttle_budget_is_images_times_image_bytes_over_the_rate():
    throttle = bf.WriteThrottle(500, sleep=_Recorder())
    expected = (256 * bf.VECTOR_ROW_BYTES) / (500 * 1024 * 1024 / 3600)
    assert throttle.budget_s(256) == pytest.approx(expected)


def test_the_cost_model_is_the_measured_row_sizes():
    # 2026-09-27, after run 36334588774: 8,440 kB / 3,584 vector rows and 5,224 kB / 9,514
    # score rows, pg_total_relation_size (heap + TOAST + every index).
    assert bf.VECTOR_ROW_BYTES == -(-8440 * 1024 // 3584)
    assert bf.SCORE_ROW_BYTES == -(-5224 * 1024 // 9514)
    # A four-to-a-page heap tuple (2,048 B) plus two 8-column B-trees: the arithmetic agrees.
    assert 2048 < bf.VECTOR_ROW_BYTES < 2048 + 2 * 250


@pytest.mark.parametrize("vectors_to, heads, expected", [
    ("postgres", False, 2412), ("postgres", True, 2412 + 563),
    ("r2", True, 563), ("r2", False, 0)])
def test_the_budget_charges_what_the_mode_writes_to_postgres(vectors_to, heads, expected):
    assert bf.bytes_per_image(vectors_to=vectors_to, score_heads=heads) == expected


def test_what_200_mb_an_hour_allows_and_why_it_did_not_bind_on_09_27():
    # Run 36334588774: 256 vectors every ~250 s. Under the old 1,552 B model a 256-chunk's
    # budget at 200 MB/h was 6.8 s, under the measured one 13.1 s: the throttle never slept,
    # so the throttle is not what held the run to ~1 image/s.
    old = bf.WriteThrottle(200, sleep=_Recorder(), image_bytes=1552)
    new = bf.WriteThrottle(200, sleep=_Recorder(),
                           image_bytes=bf.bytes_per_image(vectors_to="postgres", score_heads=True))
    assert old.budget_s(256) == pytest.approx(6.82, abs=0.01)
    assert new.budget_s(256) == pytest.approx(13.07, abs=0.01)
    assert new.pace(256, elapsed_s=250.0) == 0.0
    assert new.images_per_hour() == pytest.approx(70_492, abs=1)
    r2 = bf.WriteThrottle(200, image_bytes=bf.bytes_per_image(vectors_to="r2", score_heads=True))
    assert r2.images_per_hour() == pytest.approx(200 * 1024 * 1024 / 563)
    assert bf.WriteThrottle(200, image_bytes=0).images_per_hour() == float("inf")


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
    _provisional(monkeypatch)
    # main() must raise the rail rather than embed against a guessed resolution/dtype.
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


# --- the device ----------------------------------------------------------------


class _FakeCuda:
    def __init__(self, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


class _FakeTorch:
    def __init__(self, gpu: bool) -> None:
        self.cuda = _FakeCuda(gpu)


def _torch(monkeypatch, *, gpu: bool) -> None:
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch(gpu))


def test_the_default_device_is_the_gpu_when_there_is_one(monkeypatch):
    # This payload runs INSIDE a rented GPU pod. Until 2026-09-08 it moved nothing to
    # CUDA, so a real run would have paid for a card and computed on the CPU — same
    # vectors, hours slower, nothing in the log saying so.
    _torch(monkeypatch, gpu=True)
    assert bf.resolve_device() == "cuda"


def test_the_default_device_falls_back_to_cpu_with_no_gpu(monkeypatch):
    _torch(monkeypatch, gpu=False)
    assert bf.resolve_device() == "cpu"


def test_an_explicit_cpu_never_probes_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)  # importing it would fail
    assert bf.resolve_device("cpu") == "cpu"


def test_an_explicit_cuda_without_a_gpu_falls_back_loudly(monkeypatch, caplog):
    _torch(monkeypatch, gpu=False)
    with caplog.at_level("WARNING"):
        assert bf.resolve_device("cuda") == "cpu"
    assert "no GPU" in caplog.text


# --- the pod's vCPUs, read at run time (B-k) ---------------------------------------
# os.cpu_count() inside a RunPod container is the HOST's count (a 4090 pod gets 6 of a
# 64+ core host), so torch's threads were sized to a machine the pod does not have.


def test_pod_vcpus_reads_runpod_then_the_cgroup_quota_then_affinity(tmp_path, monkeypatch):
    monkeypatch.setattr(bf.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False)
    empty = tmp_path / "none"
    assert bf.pod_vcpus({"RUNPOD_CPU_COUNT": "6"}, str(empty)) == 6
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "cpu.max").write_text("900000 100000\n")
    assert bf.pod_vcpus({}, str(v2)) == 9
    (v2 / "cpu.max").write_text("max 100000\n")
    assert bf.pod_vcpus({}, str(v2)) == 64
    v1 = tmp_path / "v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("1600000\n")
    (v1 / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    assert bf.pod_vcpus({"RUNPOD_CPU_COUNT": "junk"}, str(v1)) == 16
    assert bf.pod_vcpus({}, str(empty)) == 64


def test_the_pod_vcpu_reader_is_g1s_wherever_both_exist(tmp_path, monkeypatch):
    g1pod = pytest.importorskip("scripts.g1_image_stack_pod")
    monkeypatch.setattr(bf.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False)
    (tmp_path / "cpu.max").write_text("600000 100000\n")
    for env in ({"RUNPOD_CPU_COUNT": "9"}, {}, {"RUNPOD_CPU_COUNT": "0"}):
        assert bf.pod_vcpus(env, str(tmp_path)) == g1pod.pod_vcpus(env, str(tmp_path))


@pytest.mark.parametrize("argv, expected", [([], 6), (["--threads", "3"], 3)])
def test_torch_threads_are_the_pods_vcpus_unless_told(monkeypatch, argv, expected):
    from scraper import dinov3_tagger

    loaded: list[dict] = []

    class _Tagger:
        revision = IDENTITY["revision"]

    def fake_load(**kwargs):
        loaded.append(kwargs)
        return _Tagger()

    monkeypatch.setenv("RUNPOD_CPU_COUNT", "6")
    monkeypatch.setattr(bf.os, "sched_getaffinity", lambda _pid: set(range(64)), raising=False)
    monkeypatch.setattr(bf.image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(bf.image_storage.R2Client, "from_env", classmethod(lambda cls, **k: object()))
    monkeypatch.setattr(dinov3_tagger.Dinov3Tagger, "load", staticmethod(fake_load))
    conn = _FakeConn(images=[(1, "img/1.jpg")], embeddings=[_emb(1)])   # nothing pending
    assert _run(monkeypatch, conn, ["--max-write-mb-per-hour", "1", "--device", "cpu",
                                    *argv]) == 0
    assert loaded == [{"threads": expected, "device": "cpu"}]


# --- scope and in-pass head scoring (G4) -----------------------------------------


def test_the_all_scope_is_the_original_anti_join_byte_for_byte():
    assert bf.pending_sql("all") == bf._PENDING_SQL


@pytest.mark.parametrize("scope, needle", [
    ("ids", "i.listing_id = any(%(listing_ids)s::bigint[])"),
    ("rt", "autodedup.rt_scope_ids"),
])
def test_a_scope_only_adds_its_listing_clause(scope, needle):
    sql = bf.pending_sql(scope)
    assert needle in sql
    assert " ".join(sql.replace(bf.SCOPES[scope], "").split()) == " ".join(bf._PENDING_SQL.split())


def test_listing_ids_file_is_read_plain_or_gz(tmp_path):
    import gzip

    plain = tmp_path / "ids.txt"
    plain.write_text("# trial\n5\n3\n\n5\n")
    packed = tmp_path / "ids.txt.gz"
    with gzip.open(packed, "wt") as handle:
        handle.write("7\n1\n")
    assert bf.read_listing_ids(str(plain)) == [3, 5]
    assert bf.read_listing_ids(str(packed)) == [1, 7]


def test_ids_scope_binds_the_listing_ids():
    conn = _RecordingConn()
    bf.select_pending(conn, identity=IDENTITY, batch=10, shard=0, shards=1, after_id=0,
                      scope="ids", listing_ids=[4, 2])
    sql, params = conn.last
    assert "listing_ids" in sql and params["listing_ids"] == [4, 2]


class _RecordingConn:
    last: tuple = ()

    def transaction(self):
        return _Transaction()

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, sql, params=None):
                if not sql.startswith("SET"):
                    conn.last = (sql, params)

            def fetchall(self):
                return []
        return _Cur()


def _provisional(monkeypatch):
    """An under-specified config file, standing in for the pre-G4 shipped one."""
    import json as _json
    import tempfile
    from pathlib import Path as _Path

    from scraper import dinov3_config

    raw = dinov3_config.load_dinov3_config()
    raw.update(resolution=None, preprocessing=None, dtype=None)
    path = _Path(tempfile.mkdtemp()) / "dinov3_config.json"
    path.write_text(_json.dumps(raw))
    monkeypatch.setattr(dinov3_config, "_CONFIG_PATH", path)


def test_blocks_scope_resolves_through_the_exports_own_resolver(monkeypatch):
    from autodedup import export

    seen = []

    def fake_fetch(conn, block, *, timeout_ms, negctl_max):
        seen.append(block.key)
        return ([3, 1] if block.grain == "town" else [2, 3]), {}

    monkeypatch.setattr(export, "fetch_block_ids", fake_fetch)
    assert bf.block_listing_ids(object(), "town:563510,quarter:490245") == [1, 2, 3]
    assert len(seen) == 2
    assert bf.pending_sql("blocks") == bf.pending_sql("ids")


# --- one pending scan per page, pod sizing, and the R2 mode (2026-09-27) -----------------
# Run 36334588774 wrote 256 vectors every ~250 s: the pending anti-join ran once per 256-image
# chunk. The throttle never slept (see the cost-model tests above).


class _Row(list):
    def tolist(self):
        return list(self)


class _FakeTagger:
    revision = IDENTITY["revision"]

    def __init__(self) -> None:
        self.prepare_threads: list[str] = []
        self.embedded = 0

    def prepare(self, img):
        import threading

        self.prepare_threads.append(threading.current_thread().name)
        return img

    def embed(self, images, batch_size=32, prepared=False):
        assert prepared, "the payload prepares images on its worker threads"
        self.embedded += len(images)
        return [_Row([(k % 7) / 64.0 + j / 4096.0 for j in range(768)])
                for k in range(len(images))]


class _FakeR2:
    def __init__(self) -> None:
        from PIL import Image
        import io as _io

        buf = _io.BytesIO()
        Image.new("RGB", (8, 6), (200, 10, 10)).save(buf, format="JPEG")
        self.jpeg = buf.getvalue()
        self.objects: dict[str, bytes] = {}
        self.log: list[tuple[str, str]] = []

    def download_bytes(self, key):
        if key.startswith("img/"):
            return self.jpeg
        return self.objects[key]

    def upload_bytes(self, key, data, content_type="image/jpeg"):
        self.log.append(("put", key))
        self.objects[key] = bytes(data)

    def list_keys(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))


def _live(monkeypatch, conn, r2, argv, scorer_log=None):
    from scraper import dinov3_tagger

    tagger = _FakeTagger()
    monkeypatch.setattr(dinov3_tagger.Dinov3Tagger, "load", staticmethod(lambda **k: tagger))
    monkeypatch.setattr(bf.image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(bf.image_storage.R2Client, "from_env", classmethod(lambda cls, **k: r2))
    monkeypatch.setenv("RUNPOD_CPU_COUNT", "4")
    monkeypatch.setenv("RUNPOD_MEM_GB", "16")
    if scorer_log is not None:
        class _Scorer:
            def __init__(self, _conn, _identity):
                self.written = 0

            def score(self, _conn, vectors):
                scorer_log.append(dict(vectors))
                r2.log.append(("score", ",".join(map(str, sorted(vectors)))))
                self.written += len(vectors)
                return len(vectors)

        monkeypatch.setattr(bf, "HeadScorer", _Scorer)
    assert _run(monkeypatch, conn, argv) == 0
    return tagger


def _selects(conn) -> int:
    return sum(1 for sql, _p in conn.executed if sql.startswith("SELECT i.id, i.storage_path"))


def _images(n: int) -> list[tuple[int, str]]:
    return [(i, f"img/{i}.jpg") for i in range(1, n + 1)]


def test_the_default_mode_is_unchanged_one_scan_per_chunk_into_postgres(monkeypatch):
    conn = _FakeConn(images=_images(10), embeddings=[])
    r2 = _FakeR2()
    tagger = _live(monkeypatch, conn, r2, ["--max-write-mb-per-hour", "200", "--chunk", "4",
                                           "--device", "cpu"])
    assert _selects(conn) == 3                       # 4 + 4 + 2: one scan per chunk, as before
    inserts = [sql for sql, _p in conn.executed if sql.startswith("INSERT INTO image_dinov3")]
    assert len(inserts) == 3
    assert [row[0] for row in conn.written] == list(range(1, 11))
    assert all(row[-1].startswith("[") and row[1:8] == tuple(IDENTITY[f] for f in IDENTITY_FIELDS)
               for row in conn.written)
    assert r2.log == []                              # nothing but image downloads touched R2
    assert tagger.embedded == 10
    assert all(name != "MainThread" for name in tagger.prepare_threads)


def test_a_select_page_pays_the_pending_scan_once(monkeypatch):
    conn = _FakeConn(images=_images(10), embeddings=[])
    _live(monkeypatch, conn, _FakeR2(), ["--max-write-mb-per-hour", "200", "--chunk", "4",
                                         "--select-page", "100", "--device", "cpu"])
    assert _selects(conn) == 1
    assert [row[0] for row in conn.written] == list(range(1, 11))


def test_the_limit_still_bounds_a_paged_pass(monkeypatch):
    conn = _FakeConn(images=_images(10), embeddings=[])
    _live(monkeypatch, conn, _FakeR2(), ["--max-write-mb-per-hour", "200", "--chunk", "4",
                                         "--select-page", "100", "--limit", "6",
                                         "--device", "cpu"])
    assert [row[0] for row in conn.written] == list(range(1, 7))
    select = next(p for sql, p in conn.executed if sql.startswith("SELECT i.id, i.storage_path"))
    assert select["batch"] == 6


def _r2_argv(*extra):
    return ["--max-write-mb-per-hour", "100", "--vectors-to", "r2", "--score-heads",
            "--chunk", "4", "--select-page", "100", "--device", "cpu", *extra]


def test_r2_mode_writes_shards_and_a_manifest_and_only_scores_reach_postgres(monkeypatch):
    from toolkit import vector_shards as vs

    conn = _FakeConn(images=_images(10), embeddings=[])
    r2 = _FakeR2()
    scored: list[dict] = []
    _live(monkeypatch, conn, r2, _r2_argv(), scorer_log=scored)
    prefix = vs.default_prefix(IDENTITY)
    assert conn.written == []                        # no vector row reached Postgres
    assert not any(sql.startswith("INSERT INTO image_dinov3") for sql, _p in conn.executed)
    assert not any("NOT EXISTS" in sql for sql, _p in conn.executed
                   if sql.startswith("SELECT i.id"))
    manifest = vs.load_manifest(r2, prefix)
    assert sorted(manifest) == list(range(1, 11))
    assert vs.count_parts(r2, prefix, shard=0, shards=1) == 3
    assert json.loads(r2.objects[f"{prefix}/identity.json"]) == vs.canonical_identity(IDENTITY)
    # Per chunk: the part, then the scores, then the manifest line that commits both.
    kinds = [kind if kind == "score" else key.split("/")[-3]
             for kind, key in r2.log if not key.endswith("identity.json")]
    assert kinds == ["parts", "score", "manifest"] * 3
    # The scores were computed from the float16 that is stored, row for row.
    reader = vs.ShardVectorReader(r2, prefix)
    for batch in scored:
        for image_id, vec in batch.items():
            assert reader([image_id])[image_id] == tuple(vec)


def test_r2_mode_resumes_from_its_manifest(monkeypatch):
    conn = _FakeConn(images=_images(10), embeddings=[])
    r2 = _FakeR2()
    _live(monkeypatch, conn, r2, _r2_argv(), scorer_log=[])
    puts = len(r2.log)
    again = _FakeConn(images=_images(12), embeddings=[])
    tagger = _live(monkeypatch, again, r2, _r2_argv(), scorer_log=[])
    assert tagger.embedded == 2                      # only the two new images
    assert len(r2.log) == puts + 3                   # one part, one score call, one manifest


def test_r2_mode_refuses_a_prefix_holding_another_population(monkeypatch):
    from toolkit import vector_shards as vs

    r2 = _FakeR2()
    prefix = vs.default_prefix(IDENTITY)
    r2.objects[f"{prefix}/identity.json"] = json.dumps(
        vs.canonical_identity({**IDENTITY, "dtype": "fp32"})).encode()
    with pytest.raises(vs.ShardStoreError, match="different population"):
        _live(monkeypatch, _FakeConn(images=_images(3), embeddings=[]), r2, _r2_argv(),
              scorer_log=[])


def test_r2_mode_honours_an_explicit_prefix(monkeypatch):
    from toolkit import vector_shards as vs

    r2 = _FakeR2()
    _live(monkeypatch, _FakeConn(images=_images(3), embeddings=[]), r2,
          _r2_argv("--r2-prefix", "runs/g4-measure/"), scorer_log=[])
    assert sorted(vs.load_manifest(r2, "runs/g4-measure")) == [1, 2, 3]


def test_an_auto_chunk_pass_sizes_itself_and_still_writes_everything(monkeypatch):
    conn = _FakeConn(images=_images(40), embeddings=[])
    _live(monkeypatch, conn, _FakeR2(), ["--max-write-mb-per-hour", "200", "--chunk", "0",
                                         "--workers", "0", "--batch-size", "0",
                                         "--select-page", "1000", "--device", "cpu"])
    assert [row[0] for row in conn.written] == list(range(1, 41))


def test_pod_ram_reads_runpod_then_the_cgroup_then_meminfo(tmp_path):
    none = tmp_path / "none"
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       263842304 kB\nMemFree: 1 kB\n")
    assert bf.pod_ram_bytes({"RUNPOD_MEM_GB": "41"}, str(none), str(meminfo)) == 41 * 1024 ** 3
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "memory.max").write_text("34359738368\n")
    assert bf.pod_ram_bytes({"RUNPOD_MEM_GB": "junk"}, str(v2), str(meminfo)) == 32 * 1024 ** 3
    (v2 / "memory.max").write_text("max\n")
    assert bf.pod_ram_bytes({}, str(v2), str(meminfo)) == 263842304 * 1024   # the HOST's
    v1 = tmp_path / "v1"
    (v1 / "memory").mkdir(parents=True)
    (v1 / "memory" / "memory.limit_in_bytes").write_text("8589934592\n")
    assert bf.pod_ram_bytes({}, str(v1), str(meminfo)) == 8 * 1024 ** 3
    assert bf.pod_ram_bytes({}, str(none), str(tmp_path / "absent")) is None


@pytest.mark.parametrize("vcpus, ram_gb, chunk, workers", [
    (6, 41, 2048, 24),     # a community 4090
    (4, 25, 2048, 16),     # an A5000
    (4, 8, 1472, 16),      # a small box: RAM-bound
    (32, 125, 2048, 48),   # workers cap at 48
    (1, 2, 256, 8),        # the worker floor
    (1, 1, 64, 8),         # the chunk floor
])
def test_plan_sizes_from_vcpus_and_ram(vcpus, ram_gb, chunk, workers):
    sizing = bf.plan_sizes(vcpus=vcpus, ram_bytes=ram_gb * 1024 ** 3, resolution=768)
    assert (sizing.chunk, sizing.workers, sizing.batch_size) == (chunk, workers, 32)
    assert sizing.chunk % 32 == 0 or sizing.chunk == bf.MIN_CHUNK


def test_the_auto_chunk_targets_a_few_minutes_of_work():
    assert bf.next_chunk(2048, 0, 0.0) == bf.FIRST_CHUNK
    assert bf.next_chunk(2048, 512, 20.0) == 2048                    # 25.6 img/s -> the ceiling
    assert bf.next_chunk(2048, 512, 512.0) == int(bf.TARGET_CHUNK_S)  # 1 img/s -> 180
    assert bf.next_chunk(2048, 64, 6400.0) == bf.MIN_CHUNK


def test_scope_sql_is_the_anti_join_without_the_anti_join():
    for scope in bf.SCOPES:
        sql = " ".join(bf.scope_sql(scope).split())
        assert "NOT EXISTS" not in sql and "image_dinov3_embeddings" not in sql
        assert " ".join(bf.SCOPES[scope].split()) in sql
        assert "ORDER BY i.id LIMIT %(batch)s" in sql
