"""Backfill image_dinov3_embeddings: one L2-normalized 768-d DINOv3 vector per stored
image, under the six-fact encoder identity in data/dinov3_config.json.

This is the PAYLOAD a GPU pod runs (scripts/dinov3_embed_dispatch.py launches the pod);
it knows nothing about RunPod and runs identically on a plain runner. See
docs/design/new-dedup/ENCODER-DECISION.md §5.5 for the execution plan this implements.

CHECKPOINT/RESUME WITH NO NEW SCHEMA. There is no marker column and none is wanted: the
target table IS the checkpoint. Pending = a stored image with no row under this EXACT
six-fact config (an anti-join), so a pod dying at 60% costs minutes, a re-run is a
no-op, and an image embedded under a DIFFERENT config is still pending under this one —
which is the whole point of the six-fact key. Progress is therefore always answerable
in SQL: count(rows for this config) / count(images.storage_path IS NOT NULL).

STREAMS, never stages. The old bake-off harness downloaded every image to local disk
first; at 10.4M images that is structurally impossible (§5.5). Each --chunk is
downloaded, decoded, embedded, written and dropped.

WRITE-RATE THROTTLE, and why --max-write-mb-per-hour has no default: Supabase gp3 disk
AUTO-EXPANDS at 90% of allocated disk and the project goes READ-ONLY at 95% with the
quota exhausted — which takes down the scrapers, the API's writes, the SPA and the
pipeline, not just this job. Disk also cannot shrink. The safe rate therefore depends
on the dashboard's live disk-utilisation reading at run time and cannot be baked into a
default, so the flag is required and the operator must look before dispatching. The
budget counts MEASURED on-disk bytes per image (VECTOR_ROW_BYTES, SCORE_ROW_BYTES), so
"200 MB/h" means 200 MB/h of table + index growth.

SELECTION ONCE, NOT PER CHUNK (2026-09-27). The pending query is a cross-table anti-join
over ~12.5 M images with ORDER BY id LIMIT; run once per 256-image chunk it cost ~220 s of
every ~250 s chunk in run 36334588774 (1 image/s on a 4090). With `--select-page N` (the pod
lane passes min(limit, 200,000)) a BOUNDED scope (ids, blocks, rt, scored) is resolved once
from its own keys (resolve_scope_pending: no ordered walk of `images` at all) and the corpus
(`all`) pays its anti-join once per N images. Without it (the hourly live step) nothing
changes: one anti-join per chunk.

VECTORS IN POSTGRES OR IN R2. `--vectors-to postgres` (the default, and what the hourly
live step uses) writes image_dinov3_embeddings. `--vectors-to r2` writes float16 shard files
plus a manifest under one R2 prefix (toolkit/vector_shards.py) and only the head scores to
Postgres: the engine reads scores, and a later head version re-scores from R2
(`tag_model score --source r2:<prefix>`). In R2 mode the manifest is the checkpoint.

RUNS ON THE GPU IT IS PAYING FOR. `--device` defaults to `cuda` when torch reports a
card and `cpu` otherwise, resolved at run time and logged. Until 2026-09-08 this script
moved nothing to CUDA at all, so a pod run would have rented a GPU and computed on its
CPU — same vectors, hours slower, and nothing in the log would have said so.

Usage:  python -m scripts.dinov3_embed_backfill --max-write-mb-per-hour 500 --limit 200000
Required: SUPABASE_DB_URL (+ R2_*, HF_TOKEN and the `clip` extra to do the work).
Requires migration 480 (PR #1296) to have been applied — the table does not exist otherwise.
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from scraper import image_storage
from scraper.dinov3_config import IDENTITY_FIELDS, encoder_identity

LOG = logging.getLogger("dinov3_embed_backfill")

# THE COST MODEL, MEASURED 2026-09-27 after GitHub run 36334588774 (3,584 vectors written,
# 9,514 head-score rows present):
#   pg_total_relation_size('image_dinov3_embeddings') = 8,440 kB / 3,584 rows = 2,412 B a row,
#     heap + TOAST + both 8-column B-trees (_pkey, _encoder_idx). The arithmetic agrees: a
#     ~1.72 kB tuple (halfvec 1,544 B + seven key columns + header) fits FOUR to an 8 kB page,
#     so the heap alone is 2,048 B a row, and each index adds ~180 B.
#   pg_total_relation_size('image_tag_scores') = 5,224 kB / 9,514 rows = 563 B a row (three
#     indexes; it includes the dead versions the run's DO UPDATE left, so it errs high).
# Until 2026-09-27 the model charged 1,552 B an image (the vector's payload alone): 1.55x
# under a vector row and 1.9x under a vector + score image. WAL comes on top, roughly the same
# volume again, and is recycled at checkpoints; the budget is the durable growth.
VECTOR_ROW_BYTES = 2412
SCORE_ROW_BYTES = 563

VECTORS_TO = ("postgres", "r2")
EMBED_DIM = 768   # the halfvec(768) column, and the width of an R2 shard row

# The pod lane's page: one pending scan covers this many images (memory ~150 B each).
MAX_SELECT_PAGE = 200_000

# The pending scan is a large anti-join during the bulk phase and the pooler's 2-min
# OLTP default is the wrong limit for it (the same reasoning as clip_tag_backfill).
SELECT_TIMEOUT_MS = 300_000

# The checkpoint. `i.id > %(after_id)s` is an IN-RUN cursor only: it stops a chunk whose
# images all failed to download (a transient R2 blip writes no rows) from being selected
# forever inside one run. A fresh run starts at 0 again, so a transient failure is
# retried on the next pass while a permanent one costs one download per run, not a wedge.
_PENDING_TEMPLATE = """
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.storage_path IS NOT NULL
      {scope}
      AND i.id > %(after_id)s
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
      AND NOT EXISTS (
        SELECT 1 FROM image_dinov3_embeddings e
        WHERE e.image_id = i.id
          AND e.model = %(model)s
          AND e.revision = %(revision)s
          AND e.library = %(library)s
          AND e.pooling = %(pooling)s
          AND e.resolution = %(resolution)s
          AND e.preprocessing = %(preprocessing)s
          AND e.dtype = %(dtype)s
      )
    ORDER BY i.id
    LIMIT %(batch)s
"""

# Which images a pass may consider. `all` is the corpus (the original lane). `ids` is a
# listing-id file shipped with the ref the pod fetches; `blocks` resolves an export's block spec
# through the export's own `fetch_block_ids` (so a re-export and the embed see ONE listing set);
# `rt` is the live lane's own scope snapshot, so the engine's population is embedded first.
SCOPES: dict[str, str] = {
    "all": "",
    "ids": "AND i.listing_id = any(%(listing_ids)s::bigint[])",
    "blocks": "AND i.listing_id = any(%(listing_ids)s::bigint[])",
    "rt": ("AND i.listing_id IN (SELECT s.listing_id FROM autodedup.rt_scope_ids s "
           "WHERE s.generation = 'rt')"),
    # The population gate (G4): the images the active model already scored from the bake-off
    # arm's own vectors. Re-embedding exactly those through the production path and re-scoring
    # them tells whether production vectors ARE the population the heads were trained on.
    "scored": ("AND i.id IN (SELECT t.image_id FROM image_tag_scores t JOIN tag_head_models m "
               "ON m.id = t.model_id AND m.status = 'active')"),
}

_PENDING_SQL = _PENDING_TEMPLATE.format(scope="")

# R2 mode: the scope's images alone. Its checkpoint is the R2 manifest, subtracted in Python,
# so there is no anti-join to pay for at all.
_SCOPE_TEMPLATE = """
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.storage_path IS NOT NULL
      {scope}
      AND i.id > %(after_id)s
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
    ORDER BY i.id
    LIMIT %(batch)s
"""


def pending_sql(scope: str = "all") -> str:
    """The pending anti-join restricted to one scope; `all` is byte-for-byte `_PENDING_SQL`."""
    return _PENDING_TEMPLATE.format(scope=SCOPES[scope])


def scope_sql(scope: str = "all") -> str:
    """The scope's stored images, no anti-join (R2 mode)."""
    return _SCOPE_TEMPLATE.format(scope=SCOPES[scope])


def read_listing_ids(path: str) -> list[int]:
    """One listing id per line, `#` comments allowed; a .gz file is read transparently."""
    import gzip

    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt") as handle:
        return sorted({int(line.split("#", 1)[0]) for line in handle
                       if line.split("#", 1)[0].strip()})


_PENDING_COUNT_SQL = """
    SELECT count(*)
    FROM images i
    WHERE i.storage_path IS NOT NULL
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
      AND NOT EXISTS (
        SELECT 1 FROM image_dinov3_embeddings e
        WHERE e.image_id = i.id
          AND e.model = %(model)s
          AND e.revision = %(revision)s
          AND e.library = %(library)s
          AND e.pooling = %(pooling)s
          AND e.resolution = %(resolution)s
          AND e.preprocessing = %(preprocessing)s
          AND e.dtype = %(dtype)s
      )
"""

_EMBEDDED_COUNT_SQL = """
    SELECT count(*)
    FROM image_dinov3_embeddings e
    WHERE e.model = %(model)s
      AND e.revision = %(revision)s
      AND e.library = %(library)s
      AND e.pooling = %(pooling)s
      AND e.resolution = %(resolution)s
      AND e.preprocessing = %(preprocessing)s
      AND e.dtype = %(dtype)s
"""

_TOTAL_IMAGES_SQL = "SELECT count(*) FROM images WHERE storage_path IS NOT NULL"

# DO NOTHING, never DO UPDATE: a different config is a different ROW (that is what the
# six-fact key means), and the same config re-embedding the same image recomputes a
# byte-identical vector — so there is no meaningful update case, only a wasted write.
_INSERT_SQL = """
    INSERT INTO image_dinov3_embeddings
      (image_id, model, revision, library, pooling, resolution, preprocessing, dtype, embedding)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::halfvec)
    ON CONFLICT (image_id, model, revision, library, pooling, resolution, preprocessing, dtype)
    DO NOTHING
"""


def bytes_per_image(*, vectors_to: str, score_heads: bool) -> int:
    """Postgres bytes one embedded image costs under this pass's mode."""
    return (VECTOR_ROW_BYTES if vectors_to == "postgres" else 0) + \
        (SCORE_ROW_BYTES if score_heads else 0)


class WriteThrottle:
    """Paces writes to a bytes/hour ceiling by sleeping between batches.

    Sleep and clock are injectable so the arithmetic is testable without waiting.
    """

    def __init__(
        self,
        mb_per_hour: float,
        *,
        sleep: Callable[[float], Any] = time.sleep,
        image_bytes: int = VECTOR_ROW_BYTES,
    ) -> None:
        if mb_per_hour <= 0:
            raise ValueError("--max-write-mb-per-hour must be > 0")
        self.mb_per_hour = float(mb_per_hour)
        self.bytes_per_second = self.mb_per_hour * 1024 * 1024 / 3600.0
        self.image_bytes = int(image_bytes)
        self._sleep = sleep
        self.slept_s = 0.0

    def images_per_hour(self) -> float:
        """The ceiling in images: what the budget allows under the measured model."""
        return float("inf") if self.image_bytes <= 0 else \
            self.bytes_per_second * 3600.0 / self.image_bytes

    def budget_s(self, images: int) -> float:
        """How long `images` worth of bytes is allowed to take at the configured rate."""
        return (images * self.image_bytes) / self.bytes_per_second

    def pace(self, images: int, elapsed_s: float) -> float:
        """Sleep off whatever of the batch's byte budget the batch did not already
        spend in wall time. Returns the delay slept (0.0 when already slower than the
        ceiling)."""
        delay = self.budget_s(images) - elapsed_s
        if delay <= 0:
            return 0.0
        self._sleep(delay)
        self.slept_s += delay
        return delay


def _vec_str(row) -> str:
    """A normalized embedding row -> pgvector's text form '[f,f,...]' (halfvec parses it)."""
    return "[" + ",".join(f"{x:.6f}" for x in row.tolist()) + "]"


def _f16_bytes(emb) -> bytes:
    """Row-major float16 bytes of an (N, dim) embedding: torch on the pod, lists in tests."""
    if hasattr(emb, "to"):
        import torch

        return emb.to(torch.float16).contiguous().cpu().numpy().tobytes()
    from toolkit.vector_shards import pack_f16

    return pack_f16(emb)


def _download_decode(r2, rows: list, workers: int,
                     prepare: Callable[[Any], Any] | None = None):
    """(decoded, failed): decoded = [(image_id, RGB image)]. A failure — transient R2
    error or bytes that will never decode — is simply left unwritten; the anti-join
    picks the image up again on the next run, and the in-run cursor stops it wedging
    this one. Nothing is staged on local disk. `prepare` (the tagger's own geometry
    transform) runs here, on the worker threads, so the chunk holds res x res images
    instead of full-size ones and the resize is no longer serial."""
    from PIL import Image  # base dep

    def _one(row):
        image_id, key = row[0], row[1]
        try:
            data = r2.download_bytes(key)
        except Exception:  # noqa: BLE001 - transient R2 error: retried next run
            return image_id, None
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
            return image_id, (prepare(img) if prepare is not None else img)
        except Exception:  # noqa: BLE001 - stored bytes won't decode
            return image_id, None

    decoded: list = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for image_id, img in pool.map(_one, rows):
            if img is None:
                failed += 1
            else:
                decoded.append((image_id, img))
    return decoded, failed


def _scalar(conn, sql: str, params: dict[str, Any] | None = None,
            timeout_ms: int = SELECT_TIMEOUT_MS) -> int:
    with conn.transaction(), conn.cursor() as cur:
        # SET is a utility statement — it cannot take a bound parameter.
        cur.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
        cur.execute(sql, params or {})
        row = cur.fetchone()
    return int(row[0]) if row else 0


def embedded_count(conn, identity: dict[str, Any],
                   timeout_ms: int = SELECT_TIMEOUT_MS) -> int:
    """Rows written under this exact six-fact identity.

    Public because it is also read from OUTSIDE the pod: this lane keeps no marker
    column, so the count of its own rows is the only progress signal the dispatcher's
    watchdog (scripts/pod_watchdog.py) has to decide whether the pod is earning its rent.
    That caller passes a SHORT `timeout_ms`: it asks once a minute beside a live
    production database, and a progress question worth queueing behind the bulk write is
    not a progress question worth asking.
    """
    return _scalar(conn, _EMBEDDED_COUNT_SQL, identity, timeout_ms=timeout_ms)


def select_pending(conn, *, identity: dict[str, Any], batch: int, shard: int,
                   shards: int, after_id: int, scope: str = "all",
                   listing_ids: list[int] | None = None) -> list[tuple[int, str]]:
    """One chunk of images with no vector under this exact six-fact identity."""
    params = {**identity, "batch": batch, "shard": shard, "shards": shards,
              "after_id": after_id}
    if scope in ("ids", "blocks"):
        params["listing_ids"] = list(listing_ids or [])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
        cur.execute(pending_sql(scope), params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def select_scope(conn, *, batch: int, shard: int, shards: int, after_id: int,
                 scope: str = "all", listing_ids: list[int] | None = None
                 ) -> list[tuple[int, str]]:
    """One page of the scope's stored images, with no vector anti-join (R2 mode)."""
    params: dict[str, Any] = {"batch": batch, "shard": shard, "shards": shards,
                              "after_id": after_id}
    if scope in ("ids", "blocks"):
        params["listing_ids"] = list(listing_ids or [])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
        cur.execute(scope_sql(scope), params)
        return [(r[0], r[1]) for r in cur.fetchall()]


# BOUNDED SCOPES ARE RESOLVED FROM THEIR SMALL SIDE (the paged mode, 2026-09-27). `ORDER BY
# i.id LIMIT n` over `images` with a selective scope filter invites the planner to walk the
# 12.5 M-row primary key until n rows match: cheap when matches are dense, a whole-table walk
# when they are sparse (9,514 scored images) or when n exceeds what is left. So the scope's
# keys are read first (a listing-id file, the rt snapshot, the scored ids), their images are
# fetched by key through images_listing_id_sequence_key / images_pkey in batches (the dump's
# own pattern), and what this identity already stored is subtracted by image-id probes.
_KEY_BATCH = 2000

_SCOPE_KEYS_SQL = {
    "scored": ("SELECT t.image_id FROM image_tag_scores t JOIN tag_head_models m "
               "ON m.id = t.model_id AND m.status = 'active'"),
    "rt": "SELECT s.listing_id FROM autodedup.rt_scope_ids s WHERE s.generation = 'rt'",
}

_IMAGES_BY_KEY_SQL = """
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.{column} = any(%(keys)s::bigint[])
      AND i.storage_path IS NOT NULL
      AND (%(shards)s = 1 OR i.id %% %(shards)s = %(shard)s)
"""

_STORED_IDS_SQL = """
    SELECT e.image_id
    FROM image_dinov3_embeddings e
    WHERE e.image_id = any(%(keys)s::bigint[])
      AND e.model = %(model)s
      AND e.revision = %(revision)s
      AND e.library = %(library)s
      AND e.pooling = %(pooling)s
      AND e.resolution = %(resolution)s
      AND e.preprocessing = %(preprocessing)s
      AND e.dtype = %(dtype)s
"""


def _batched(conn, sql: str, keys: list[int], params: dict[str, Any]) -> list[tuple]:
    out: list[tuple] = []
    for i in range(0, len(keys), _KEY_BATCH):
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
            cur.execute(sql, {**params, "keys": keys[i:i + _KEY_BATCH]})
            out.extend(cur.fetchall())
    return out


def resolve_scope_pending(conn, *, scope: str, shard: int, shards: int,
                          listing_ids: list[int] | None = None,
                          identity: dict[str, Any] | None = None,
                          done: set[int] | None = None) -> list[tuple[int, str]]:
    """Every pending (image id, storage path) of a bounded scope's shard, in id order.
    `identity` subtracts the vectors Postgres holds under it; `done` subtracts ids the
    caller already has (R2 mode's manifest)."""
    if scope == "all":
        raise ValueError("the corpus is not a bounded scope; page it with select_pending")
    if scope in _SCOPE_KEYS_SQL:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = {int(SELECT_TIMEOUT_MS)}")
            cur.execute(_SCOPE_KEYS_SQL[scope])
            keys = sorted({int(r[0]) for r in cur.fetchall()})
    else:
        keys = sorted({int(k) for k in listing_ids or []})
    column = "id" if scope == "scored" else "listing_id"
    rows = sorted({(int(r[0]), r[1]) for r in _batched(
        conn, _IMAGES_BY_KEY_SQL.format(column=column), keys,
        {"shard": shard, "shards": shards})})
    skip = set(done or ())
    if identity is not None and rows:
        skip |= {int(r[0]) for r in _batched(conn, _STORED_IDS_SQL, [r[0] for r in rows],
                                             dict(identity))}
    return [r for r in rows if r[0] not in skip]


class HeadScorer:
    """Scores the vectors a chunk just wrote with the ACTIVE tag model, in the same process, so
    `image_tag_scores` fills beside the vectors (one download, one job). Refuses unless the active
    model's seven encoder facts equal the vectors' own: heads read a population, not a width."""

    def __init__(self, conn, identity: dict[str, Any]) -> None:
        from toolkit import tag_models as tm

        model = tm.active_model(conn)
        if model is None:
            raise RuntimeError("--score-heads: no active tag model to score with")
        if model.encoder.as_dict() != {**identity, "resolution": int(identity["resolution"])}:
            raise RuntimeError(
                f"--score-heads: active model {model.version} was trained on "
                f"{model.encoder.as_dict()} but this pass writes {identity} - a new population")
        self._tm, self.model, self.written = tm, model, 0

    def score(self, conn, vectors: dict[int, Any]) -> int:
        from toolkit import tag_heads as th

        source = self._tm.ScoringSource(name="embed-pass", candidates=lambda _a, _l: [],
                                        vectors=th.mapping_vector_source(vectors))
        report = self._tm.score(conn, model=self.model, source=source,
                                image_ids=list(vectors), force=True)
        self.written += report.written
        return report.written


def block_listing_ids(conn, spec: str) -> list[int]:
    """An export block spec (`town:563510,quarter:490245`; commas or spaces between blocks) ->
    the listing ids a fresh export of it would carry, by the export's own resolver."""
    from autodedup import cohort
    from autodedup.export import ARG_DEFAULTS, fetch_block_ids

    ids: set[int] = set()
    for block in cohort.parse_blocks(spec.replace(",", " ")):
        found, _detail = fetch_block_ids(conn, block, timeout_ms=SELECT_TIMEOUT_MS,
                                         negctl_max=int(ARG_DEFAULTS["negctl_max"]))
        ids.update(int(i) for i in found)
    return sorted(ids)


def _cgroup_cpus(root: str) -> float | None:
    """The container's CPU quota (cgroup v2 `cpu.max`, else v1 CFS), None when unlimited."""
    try:
        with open(os.path.join(root, "cpu.max")) as fh:
            quota, period = fh.read().split()[:2]
        if quota != "max" and int(period) > 0:
            return int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:
        with open(os.path.join(root, "cpu", "cpu.cfs_quota_us")) as fh:
            quota_us = int(fh.read())
        with open(os.path.join(root, "cpu", "cpu.cfs_period_us")) as fh:
            period_us = int(fh.read())
        if quota_us > 0 and period_us > 0:
            return quota_us / period_us
    except (OSError, ValueError):
        pass
    return None


def pod_vcpus(env: dict[str, str] | None = None, cgroup_root: str = "/sys/fs/cgroup") -> int:
    """The vCPUs this pod may use (G1's g1_image_stack_pod.pod_vcpus at bbd7e5ae).
    os.cpu_count() is the HOST's count inside a RunPod container (a 4090 pod gets 6 of a
    64+ core host), so RunPod's RUNPOD_CPU_COUNT wins, then the cgroup quota, then the
    affinity mask."""
    env = os.environ if env is None else env
    try:
        visible = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        visible = os.cpu_count() or 1
    raw = str(env.get("RUNPOD_CPU_COUNT", "")).strip()
    if raw.isdigit() and int(raw) > 0:
        return min(int(raw), visible)
    quota = _cgroup_cpus(cgroup_root)
    if quota:
        return max(1, min(visible, int(quota)))
    return max(1, visible)


def pod_ram_bytes(env: dict[str, str] | None = None, cgroup_root: str = "/sys/fs/cgroup",
                  meminfo: str = "/proc/meminfo") -> int | None:
    """The RAM this pod may use. /proc/meminfo is the HOST's inside a container, so
    RunPod's RUNPOD_MEM_GB wins, then the cgroup limit (v2 memory.max, v1
    memory.limit_in_bytes), then MemTotal; None when nothing is readable."""
    env = os.environ if env is None else env
    try:
        gb = float(str(env.get("RUNPOD_MEM_GB", "")).strip())
        if gb > 0:
            return int(gb * 1024 ** 3)
    except ValueError:
        pass
    for path in (os.path.join(cgroup_root, "memory.max"),
                 os.path.join(cgroup_root, "memory", "memory.limit_in_bytes")):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
            if raw.isdigit() and 0 < int(raw) < 1 << 60:
                return int(raw)
        except OSError:
            pass
    try:
        with open(meminfo) as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


# Sizing, per pod. One prepared image is res x res x 3 bytes (1.77 MB at 768 px) and the
# chunk holds them all; each worker also has one full-size decode in flight (~12 MP x 3 B
# plus its JPEG, ~48 MB). The chunk gets 40% of RAM after that, the rest is torch, the
# model and slack. The chunk is also TIME-bounded (TARGET_CHUNK_S): it sets the watchdog's
# progress cadence (stall deadline 900 s) and how far a pass overshoots --max-seconds.
# Batch stays 32, the bake-off's own forward batch: the GPU is not the bottleneck (12 img/s
# is ~7 TFLOPS on a 3090), and a batch change can move bf16 numerics by a kernel choice.
WORKER_INFLIGHT_BYTES = 48 * 1024 ** 2
CHUNK_RAM_SHARE = 0.4
MIN_CHUNK, MAX_CHUNK = 64, 2048
FIRST_CHUNK = 512
TARGET_CHUNK_S = 180.0
FORWARD_BATCH = 32
ASSUMED_RAM_BYTES = 16 * 1024 ** 3


@dataclass(frozen=True)
class Sizing:
    chunk: int          # the memory ceiling; the time target may use less
    batch_size: int
    workers: int


def plan_sizes(*, vcpus: int, ram_bytes: int | None, resolution: int) -> Sizing:
    """Chunk, forward batch and download workers from what the pod actually has."""
    workers = max(8, min(48, 4 * max(1, vcpus)))
    ram = ram_bytes or ASSUMED_RAM_BYTES
    room = CHUNK_RAM_SHARE * ram - workers * WORKER_INFLIGHT_BYTES
    by_ram = int(room // (resolution * resolution * 3)) // FORWARD_BATCH * FORWARD_BATCH
    return Sizing(chunk=max(MIN_CHUNK, min(MAX_CHUNK, by_ram)), batch_size=FORWARD_BATCH,
                  workers=workers)


def next_chunk(ceiling: int, images: int, seconds: float) -> int:
    """The next auto chunk: what the last one's rate does in TARGET_CHUNK_S, inside
    [MIN_CHUNK, ceiling]."""
    if images <= 0 or seconds <= 0:
        return min(ceiling, FIRST_CHUNK)
    return max(MIN_CHUNK, min(ceiling, int(images / seconds * TARGET_CHUNK_S)))


def resolve_device(requested: str = "") -> str:
    """The torch device to run the forward pass on, measured rather than assumed.

    An empty request means "use the GPU if there is one". A GPU pass and a CPU pass
    produce the same vectors, so nothing here fails loudly on its own — the run just
    takes hours and bills for a card it never touched. Hence a live
    `torch.cuda.is_available()` probe and a logged answer.
    """
    if requested and requested != "cuda":
        return requested
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if requested == "cuda":
        LOG.warning("--device=cuda but torch reports no GPU — falling back to cpu. "
                    "This will be very slow.")
    return "cpu"


def _r2_dry_run(identity: dict[str, Any], prefix: str) -> None:
    from toolkit import vector_shards as vs

    if not image_storage.is_configured():
        LOG.info("DINOV3 dry_run vectors_to=r2 prefix=%s (R2 env vars missing: not read)", prefix)
        return
    store = image_storage.R2Client.from_env()
    stored = vs.read_identity(store, prefix)
    manifest = vs.load_manifest(store, prefix)
    LOG.info("DINOV3 dry_run vectors_to=r2 prefix=%s r2_vectors=%d identity=%s",
             prefix, len(manifest), "unset" if stored is None
             else ("match" if vs.canonical_identity(stored) == vs.canonical_identity(identity)
                   else f"MISMATCH {stored}"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-write-mb-per-hour", type=float, required=True,
                   help="REQUIRED, no default. Ceiling on the Postgres bytes (table + indexes, "
                        f"measured: a vector row {VECTOR_ROW_BYTES} B, a head-score row "
                        f"{SCORE_ROW_BYTES} B) this pass adds per hour. "
                        "The safe value depends on the Supabase dashboard's LIVE disk-"
                        "utilisation reading at run time: gp3 disk auto-expands at 90%% of "
                        "allocated disk and the project goes READ-ONLY at 95%% with the "
                        "quota exhausted — which takes the scrapers, the API's writes, the "
                        "SPA and the pipeline down with it, not just this job. Disk cannot "
                        "shrink. Look at the dashboard, then pass a number.")
    p.add_argument("--limit", type=int, default=200_000, help="Max images per run.")
    p.add_argument("--chunk", type=int, default=256,
                   help="Images per download+embed+commit cycle (bounds memory). 0 = sized "
                        "from the pod's RAM and re-sized to ~TARGET_CHUNK_S of work.")
    p.add_argument("--batch-size", type=int, default=32,
                   help="Model forward batch. 0 = the sizing default (32).")
    p.add_argument("--workers", type=int, default=16,
                   help="Parallel R2 downloads (+ decode + resize). 0 = 4 per vCPU, 8..48.")
    p.add_argument("--select-page", type=int, default=0,
                   help="0 = one pending anti-join per chunk (the pre-09-27 behaviour, kept "
                        "for the hourly step). >0 = a bounded scope is resolved ONCE from its "
                        "keys and the corpus scope is paged N images per anti-join (the pod "
                        f"lane passes min(limit, {MAX_SELECT_PAGE:,})).")
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1, help="image_id %% shards == shard.")
    p.add_argument("--threads", type=int, default=0,
                   help="torch threads (0 = the vCPUs this pod or runner may use, read at "
                        "run time: RUNPOD_CPU_COUNT, else the cgroup quota, else affinity).")
    p.add_argument("--device", default="",
                   help="Torch device for the forward pass. Empty (the default) "
                        "resolves to 'cuda' when torch reports a GPU and 'cpu' "
                        "otherwise, and the resolved value is logged — this job runs "
                        "inside a rented GPU pod, so a silent CPU pass would pay for "
                        "a GPU and never touch it.")
    p.add_argument("--max-seconds", type=float, default=0,
                   help="Time budget; stop cleanly at a chunk boundary. 0 = unbounded. "
                        "Set it under the runner/pod timeout so the pass reports what it "
                        "wrote instead of being killed mid-flight.")
    p.add_argument("--scope", choices=sorted(SCOPES), default="all",
                   help="all = the corpus; rt = the live lane's scope snapshot; ids = the "
                        "listings in --listing-ids-file; scored = the images the active tag "
                        "model already scored (the population gate).")
    p.add_argument("--listing-ids-file", default="",
                   help="With --scope ids: a file (optionally .gz) of listing ids, one per "
                        "line, in the ref the pod fetched.")
    p.add_argument("--blocks", default="",
                   help="With --scope blocks: export block specs joined by ',', e.g. "
                        "town:563510+town:577626+quarter:490245.")
    p.add_argument("--score-heads", action="store_true",
                   help="Also score each written vector with the ACTIVE tag model and upsert "
                        "image_tag_scores in the same pass (refused unless the model's encoder "
                        "identity equals this pass's).")
    p.add_argument("--vectors-to", choices=VECTORS_TO, default="postgres",
                   help="postgres = image_dinov3_embeddings (the default; the live hourly "
                        "step). r2 = float16 shard files + manifest under --r2-prefix, and only "
                        "the head scores reach Postgres; the manifest is the checkpoint.")
    p.add_argument("--r2-prefix", default="",
                   help="With --vectors-to r2: the store's prefix. Empty = one per encoder "
                        "identity (toolkit.vector_shards.default_prefix), shared by every "
                        "scope and dispatch, so a re-dispatch resumes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the resolved encoder identity and the pending count, then "
                        "exit. Downloads nothing, embeds nothing, writes nothing.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    # Raises unless all six facts are set — the refuse-to-run rail. Deliberately BEFORE
    # the R2 guard: an under-specified encoder is a hard error, not a skip.
    identity = encoder_identity()
    LOG.info("DINOV3 identity %s",
             " ".join(f"{k}={identity[k]}" for k in IDENTITY_FIELDS))
    r2_mode = args.vectors_to == "r2"
    prefix = ""
    if r2_mode:
        from toolkit import vector_shards as vs

        prefix = args.r2_prefix.strip("/") or vs.default_prefix(identity)

    listing_ids: list[int] | None = None
    if args.scope == "ids":
        if not args.listing_ids_file:
            print("ERROR: --scope ids needs --listing-ids-file.", file=sys.stderr)
            return 2
        listing_ids = read_listing_ids(args.listing_ids_file)
        LOG.info("DINOV3 scope=ids listings=%d file=%s", len(listing_ids), args.listing_ids_file)

    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        if args.scope == "blocks":
            if not args.blocks:
                print("ERROR: --scope blocks needs --blocks.", file=sys.stderr)
                return 2
            listing_ids = block_listing_ids(conn, args.blocks)
            LOG.info("DINOV3 scope=blocks listings=%d blocks=%s", len(listing_ids), args.blocks)
        total_images = _scalar(conn, _TOTAL_IMAGES_SQL)
        embedded_before = _scalar(conn, _EMBEDDED_COUNT_SQL, identity)
        if args.dry_run:
            if r2_mode:
                _r2_dry_run(identity, prefix)
                return 0
            pending = _scalar(conn, _PENDING_COUNT_SQL,
                              {**identity, "shard": args.shard, "shards": args.shards})
            LOG.info("DINOV3 dry_run pending=%d shard=%d/%d embedded=%d/%d (%.2f%%) "
                     "max_write_mb_per_hour=%.1f",
                     pending, args.shard, args.shards, embedded_before, total_images,
                     100.0 * embedded_before / total_images if total_images else 0.0,
                     args.max_write_mb_per_hour)
            return 0

        if not image_storage.is_configured():
            LOG.info("DINOV3 skip: R2 env vars missing")
            return 0

        from scraper.dinov3_tagger import Dinov3Tagger

        scorer = HeadScorer(conn, identity) if args.score_heads else None

        device = resolve_device(args.device)
        vcpus = pod_vcpus()
        threads = args.threads or vcpus
        sizing = plan_sizes(vcpus=vcpus, ram_bytes=pod_ram_bytes(),
                            resolution=int(identity["resolution"]))
        auto_chunk = args.chunk <= 0
        chunk_ceiling = sizing.chunk if auto_chunk else args.chunk
        batch_size = args.batch_size if args.batch_size > 0 else sizing.batch_size
        workers = args.workers if args.workers > 0 else sizing.workers
        LOG.info("DINOV3 device=%s (requested=%s) threads=%d host_cpu_count=%s workers=%d "
                 "chunk=%s batch=%d select_page=%s vectors_to=%s%s",
                 device, args.device or "auto", threads, os.cpu_count(), workers,
                 f"auto<={chunk_ceiling}" if auto_chunk else chunk_ceiling, batch_size,
                 args.select_page or "chunk", args.vectors_to,
                 f" prefix={prefix}" if r2_mode else "")
        tagger = Dinov3Tagger.load(threads=threads, device=device)
        # Stamp what the LOADED weights actually were, never the file we read — a tagger
        # loaded some other way can then never write a row claiming a revision it did
        # not use (the rail clip_tag_backfill.py already applies to CLIP).
        write_identity = {**identity, "revision": tagger.revision}
        throttle = WriteThrottle(args.max_write_mb_per_hour,
                                 image_bytes=bytes_per_image(vectors_to=args.vectors_to,
                                                             score_heads=scorer is not None))
        LOG.info("DINOV3 write budget %.0f MB/h at %d B an image = %.0f images/h",
                 args.max_write_mb_per_hour, throttle.image_bytes, throttle.images_per_hour())
        r2 = image_storage.R2Client.from_env(max_pool_connections=workers + 4)
        writer = None
        done: set[int] = set()
        if r2_mode:
            vs.ensure_identity(r2, prefix, write_identity)
            writer = vs.ShardWriter(store=r2, prefix=prefix, shard=args.shard,
                                    shards=args.shards, dim=EMBED_DIM)
            done = set(vs.load_manifest(r2, prefix))
            embedded_before = len(done)
            LOG.info("DINOV3 r2 prefix=%s already_stored=%d", prefix, len(done))
        deadline = time.monotonic() + args.max_seconds if args.max_seconds else None

        written = failed = seen = 0
        after_id = 0
        select_s = 0.0
        page: list[tuple[int, str]] = []
        page_exhausted = False
        chunk = min(chunk_ceiling, FIRST_CHUNK) if auto_chunk else chunk_ceiling
        stopped = "drained"
        if args.select_page > 0 and args.scope != "all":
            t_sel = time.monotonic()
            page = resolve_scope_pending(
                conn, scope=args.scope, shard=args.shard, shards=args.shards,
                listing_ids=listing_ids, identity=None if r2_mode else identity,
                done=done)[:args.limit]
            page_exhausted = True
            select_s = time.monotonic() - t_sel
            LOG.info("DINOV3 scope=%s resolved from its keys: pending=%d shard=%d/%d in %.1fs",
                     args.scope, len(page), args.shard, args.shards, select_s)
        last_pace = time.monotonic()
        while seen < args.limit:
            if deadline and time.monotonic() >= deadline:
                stopped = "time-budget"
                break
            if not page:
                if page_exhausted:
                    break
                want = min(args.select_page or chunk, args.limit - seen)
                t_sel = time.monotonic()
                if r2_mode:
                    fetched = select_scope(conn, batch=want, shard=args.shard,
                                           shards=args.shards, after_id=after_id,
                                           scope=args.scope, listing_ids=listing_ids)
                else:
                    fetched = select_pending(conn, identity=identity, batch=want,
                                             shard=args.shard, shards=args.shards,
                                             after_id=after_id, scope=args.scope,
                                             listing_ids=listing_ids)
                select_s += time.monotonic() - t_sel
                if not fetched:
                    break
                after_id = max(r[0] for r in fetched)
                page_exhausted = len(fetched) < want
                page = [r for r in fetched if r[0] not in done]
                if not page:
                    continue
            rows, page = page[:chunk], page[chunk:]
            seen += len(rows)

            t0 = time.monotonic()
            decoded, chunk_failed = _download_decode(r2, rows, workers, tagger.prepare)
            failed += chunk_failed
            chunk_written = 0
            if decoded:
                ids = [image_id for image_id, _img in decoded]
                emb = tagger.embed([d[1] for d in decoded], batch_size, prepared=True)
                if writer is not None:
                    f16 = _f16_bytes(emb)
                    part = writer.put_part(ids, f16)
                    if scorer is not None:
                        # Scored from the float16 that is STORED, so a re-score of the same
                        # model from R2 reproduces these numbers exactly.
                        stored = vs.Part(image_ids=tuple(ids), dim=writer.dim, raw=f16)
                        scorer.score(conn, {i: stored.vector(row) for row, i in enumerate(ids)})
                    writer.commit(ids, part)
                else:
                    params = [
                        (image_id, write_identity["model"], write_identity["revision"],
                         write_identity["library"], write_identity["pooling"],
                         write_identity["resolution"], write_identity["preprocessing"],
                         write_identity["dtype"], _vec_str(emb[i]))
                        for i, image_id in enumerate(ids)
                    ]
                    with conn.cursor() as cur:
                        cur.executemany(_INSERT_SQL, params)
                    if scorer is not None:
                        scorer.score(conn, {image_id: [float(x) for x in emb[i].tolist()]
                                            for i, image_id in enumerate(ids)})
                chunk_written = len(ids)
                written += chunk_written
            elapsed = time.monotonic() - t0

            # Progress is a Postgres (or manifest) fact, not a local counter: every
            # increment is a committed row or a committed part. The full count is NOT
            # re-read per chunk — it is a 10M-row scan — but it is re-read at the end.
            slept = throttle.pace(chunk_written, time.monotonic() - last_pace)
            last_pace = time.monotonic()
            LOG.info("DINOV3 progress embedded=%d/%d (%.2f%%) run_written=%d seen=%d/%d "
                     "failed=%d chunk=%d chunk_s=%.1f img_s=%.1f select_s=%.1f slept_s=%.1f",
                     embedded_before + written, total_images,
                     100.0 * (embedded_before + written) / total_images if total_images else 0.0,
                     written, seen, args.limit, failed, len(rows), elapsed,
                     len(rows) / elapsed if elapsed > 0 else 0.0, select_s, slept)
            if auto_chunk:
                chunk = next_chunk(chunk_ceiling, len(rows), elapsed)

        embedded_after = (len(vs.load_manifest(r2, prefix)) if r2_mode
                          else _scalar(conn, _EMBEDDED_COUNT_SQL, identity))

    LOG.info("DINOV3 done stop=%s run_written=%d failed=%d embedded=%d/%d (%.2f%%) "
             "throttle_slept_s=%.0f select_s=%.0f heads_scored=%d scope=%s vectors_to=%s%s",
             stopped, written, failed, embedded_after, total_images,
             100.0 * embedded_after / total_images if total_images else 0.0,
             throttle.slept_s, select_s, scorer.written if scorer is not None else 0,
             args.scope, args.vectors_to, f" prefix={prefix}" if r2_mode else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
