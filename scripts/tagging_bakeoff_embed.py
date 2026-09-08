"""Embed one tagging bake-off run's images under every pending arm — the pod payload.

Stage 2 of 3. This is what a rented GPU pod runs; it knows nothing about RunPod and runs
identically on any box with a GPU, or on a CPU if you are patient.
`scripts/tagging_bakeoff_dispatch.py` is the half that rents the hardware.

WHAT IT DOES, per arm: resolve the checkpoint's commit sha at run time, load the encoder
PINNED to it, stream this run's images through it, and write one L2-normalised vector
per image into `dedup_sim.tag_head_bakeoff_vectors`. Then stamp the arm's `dim` and its
terminal `status`.

DOWNLOAD ONCE, EMBED N TIMES. The manifest's ~10.8k presigned URLs are fetched to a
local cache directory before the first arm runs, and every later arm reads the same
bytes off disk. Ten arms downloading the corpus ten times would spend most of the pod's
life on R2 egress rather than on the GPU — the exact starvation ENCODER-DECISION §5.3
warns about, arrived at from the other direction. ~1 GB of cache buys it back. (The
PRODUCTION corpus job streams instead and must: 10.4M images do not fit on a pod's disk.
A 10.8k bake-off is a different problem and gets a different answer.)

RESUMABLE WITHOUT A MARKER COLUMN, the same way the production lane is: the vectors
table IS the checkpoint. An arm's already-written image_ids are read once into a set and
skipped, so a pod dying at arm 6 of 10 costs the arms it had not started. Re-running is
a no-op — including an arm a killed pod left `running`, which `pending_arms` picks up
again precisely because the VECTORS, not the status, are the record of work done.

IT HEARTBEATS INTO ITS OWN ROWS, AND THE FIRST DB WRITE IS THE BOOT STAMP — before the
manifest, before a single weight byte. The dispatcher's watchdog
(`scripts/pod_watchdog.py`) reads these rows to decide whether to tear the pod down
early, and without a boot stamp it cannot tell "the clone or the install failed" (the
2026-09-08 failure: $0.50 for zero vectors) from "the weights are still downloading". No
migration: the boot/alive lines live in the run row's `note`, per-arm progress in the arm
row's `note`, both carrying the resolved python/torch/transformers versions.

A GATED ARM WITH NO RESOLVABLE REVISION IS SKIPPED, NEVER LOADED. `status='skipped'`
with the reason in `note`. Loading `main` unpinned would write vectors nothing can ever
identify — and, worse, would look like a successful arm in the results table.

POOLING IS PER FAMILY, NOT ONE HARDCODED SLICE. The DINO arms go through
`scraper.dinov3_tagger.Dinov3Tagger` (post-LayerNorm CLS, `model.eval()`, our own
geometry with the processor's resize/crop switched off). SigLIP2 has NO CLS token at all
— its summary comes from a learned attention-pooling head — and CLIP's comparable vector
is the PROJECTED `image_embeds`, not any hidden state. Those two get the minimal loader
below, which reuses the DINO module's geometry transforms and dtype rules so the arms
differ only where they are meant to.

Usage:  python -m scripts.tagging_bakeoff_embed --run-id 7 --device cuda
Required: SUPABASE_DB_URL, R2_* (to fetch the manifest), HF_TOKEN (gated arms),
and the `clip` extra for torch + transformers.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from scraper import dinov3_tagger, image_storage
from scripts import pod_bootstrap
from scripts import tagging_bakeoff_arms as arms_mod

LOG = logging.getLogger("tagging_bakeoff_embed")

# On the CONTAINER disk, beside the bootstrap's own work dir — never `/workspace`, which
# is the pod VOLUME's mount point and which these lanes no longer rent (2026-09-08 (h)).
DEFAULT_CACHE_DIR = f"{pod_bootstrap.CONTAINER_ROOT}/tagging-bakeoff-cache"
# Big enough that the per-statement overhead disappears, small enough that a killed pod
# loses at most this much work on the arm it was running.
WRITE_BATCH = 500
# How often the cache phase stamps the run note. The dispatcher's stall deadline is
# minutes and downloading ~10.8k images takes longer than that gap between vector
# writes, so the phase that writes no vectors still has to say it is alive.
CACHE_HEARTBEAT_EVERY = 250

# The two lines this payload OWNS inside `tag_head_bakeoff_runs.note`. Rewritten, never
# appended to, so a re-dispatch cannot be mistaken for the previous one's boot — the
# dispatcher compares the stamp against its own launch time.
BOOT_PREFIX = "pod booted "
ALIVE_PREFIX = "pod alive "

_RUN_SQL = """
    SELECT id, label, status, manifest_key
    FROM dedup_sim.tag_head_bakeoff_runs
    WHERE id = %(run_id)s
"""

_ARMS_SQL = """
    SELECT id, arm, model, revision, library, pooling, resolution, preprocessing,
           dtype, status
    FROM dedup_sim.tag_head_bakeoff_arms
    WHERE run_id = %(run_id)s
    ORDER BY id
"""

_DONE_IDS_SQL = """
    SELECT image_id FROM dedup_sim.tag_head_bakeoff_vectors WHERE arm_id = %(arm_id)s
"""

# DO NOTHING, never DO UPDATE: an (arm, image) pair is one deterministic vector, so a
# second write of the same pair is either identical or a bug — and an UPDATE would hide
# the second case. Same reasoning as the production backfill's insert.
_INSERT_VECTOR_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_vectors (arm_id, image_id, embedding)
    VALUES (%s, %s, %s::halfvec)
    ON CONFLICT DO NOTHING
"""

_START_ARM_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms
    SET status = 'running', revision = %(revision)s
    WHERE id = %(arm_id)s
"""

_FINISH_ARM_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms
    SET status = %(status)s, dim = COALESCE(%(dim)s, dim), note = %(note)s
    WHERE id = %(arm_id)s
"""

_SKIP_ARM_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms
    SET status = 'skipped', note = %(note)s
    WHERE id = %(arm_id)s
"""

# The per-arm heartbeat. `status='running'` is restated on purpose: an arm a killed pod
# left `running` is resumable, so the status alone never means "someone is working on
# it" — the timestamp in the note is what says that.
_HEARTBEAT_ARM_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms
    SET status = 'running', note = %(note)s
    WHERE id = %(arm_id)s
"""

_READ_RUN_NOTE_SQL = """
    SELECT note FROM dedup_sim.tag_head_bakeoff_runs WHERE id = %(run_id)s
"""

_WRITE_RUN_NOTE_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_runs SET note = %(note)s WHERE id = %(run_id)s
"""


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runtime_versions() -> str:
    """The interpreter and libraries this pod actually resolved.

    Recorded, not pinned: the pod bootstraps its own 3.12 and installs whatever the
    cu118 index currently offers (`scripts/pod_bootstrap.py`), so the only honest record
    of what ran is the one written at run time into the rows the run is judged by.
    """
    parts = [f"py{sys.version.split()[0]}"]
    for name in ("torch", "transformers"):
        try:
            module = __import__(name)
            parts.append(f"{name}{getattr(module, '__version__', '?')}")
        except Exception:  # noqa: BLE001 - a missing library IS the record here
            parts.append(f"{name}=absent")
    return " ".join(parts)


def stamp_run(conn: Any, *, run_id: int, boot: str | None = None,
              alive: str | None = None) -> None:
    """Rewrite this payload's heartbeat lines in the run note, leaving the manifest
    stage's own note intact. Read-modify-write is safe here: one pod writes this row."""
    with conn.cursor() as cur:
        cur.execute(_READ_RUN_NOTE_SQL, {"run_id": run_id})
        row = cur.fetchone()
    note = (row[0] if row else None) or ""
    lines = [
        line for line in note.splitlines()
        if not line.startswith(ALIVE_PREFIX)
        and not (boot is not None and line.startswith(BOOT_PREFIX))
    ]
    # The manifest's note is trimmed if anything has to give: the heartbeat is what the
    # watchdog reads, and a truncated one would read as a pod that never booted.
    kept = "\n".join(lines)[:3000]
    managed = []
    if boot is not None:
        managed.append(f"{BOOT_PREFIX}{iso_now()} {boot}")
    if alive is not None:
        managed.append(f"{ALIVE_PREFIX}{iso_now()} {alive}")
    with conn.cursor() as cur:
        cur.execute(_WRITE_RUN_NOTE_SQL,
                    {"run_id": run_id,
                     "note": "\n".join([kept, *managed] if kept else managed)})


class SimpleEncoder:
    """The minimal non-DINO loader: SigLIP2's attention-pool head and CLIP's projected
    `image_embeds`, both at an EXPLICIT revision.

    Deliberately thin. Geometry, dtype resolution and the "the processor normalizes, it
    does not resize" rule all come from `scraper.dinov3_tagger`, so a SigLIP2 arm and a
    DINOv3 arm differ in exactly the things the bake-off is measuring and in nothing
    else.
    """

    def __init__(self, model, processor, *, pooling: str, resolution: int,
                 preprocessing: str, torch_dtype, device: str, revision: str) -> None:
        self._model = model
        self._processor = processor
        self._torch_dtype = torch_dtype
        self._device = device
        self.pooling = pooling
        self.resolution = int(resolution)
        self.preprocessing = preprocessing
        self.revision = revision

    @classmethod
    def load(cls, *, model_id: str, revision: str, model_class: str, pooling: str,
             resolution: int, preprocessing: str, dtype: str,
             device: str) -> "SimpleEncoder":
        import transformers

        torch_dtype = dinov3_tagger.resolve_torch_dtype(dtype)
        token = os.environ.get("HF_TOKEN")
        cls_ = getattr(transformers, model_class, None) or transformers.AutoModel
        dtype_kwargs: dict[str, Any] = ({"dtype": torch_dtype}
                                        if torch_dtype is not None else {})
        # `revision=` stays an explicit keyword at BOTH call sites — a pin that only
        # exists inside a **kwargs dict is a pin no AST sweep can see.
        model = cls_.from_pretrained(model_id, revision=revision, token=token,
                                     **dtype_kwargs)
        processor = transformers.AutoImageProcessor.from_pretrained(
            model_id, revision=revision, token=token)
        model.eval()
        if device != "cpu":
            model.to(device)
        return cls(model, processor, pooling=pooling, resolution=resolution,
                   preprocessing=preprocessing, torch_dtype=torch_dtype, device=device,
                   revision=revision)

    def _pool(self, outputs):
        if self.pooling == "attention_pool":
            # SigLIP2 has no CLS token; `pooler_output` here IS the learned attention
            # head's output, a different mechanism from the DINO arms' post-LN CLS that
            # happens to share the attribute name.
            pooled = getattr(outputs, "pooler_output", None)
        elif self.pooling == "image_embeds":
            pooled = getattr(outputs, "image_embeds", None)
        else:
            raise RuntimeError(
                f"SimpleEncoder does not implement pooling {self.pooling!r} — the DINO "
                "family goes through scraper.dinov3_tagger.Dinov3Tagger instead")
        if pooled is None:
            raise RuntimeError(
                f"model output has no usable tensor for pooling={self.pooling!r}")
        return pooled

    def _process(self, batch: list):
        """Normalize + tensorize, with the processor's OWN geometry switched off.

        The geometry is already ours (`apply_preprocessing` produced a
        resolution-square), so the processor must only normalize. Not every family's
        processor accepts both switches — SigLIP's has no centre crop to disable — so an
        unknown-kwarg rejection falls back rather than failing the arm. That fallback is
        safe ONLY because these two arms run at their checkpoint's NATIVE resolution, so
        the processor's default resize is an identity on an already-square image of that
        size. It would not be safe on a resolution arm, which is why the DINO family
        (where the resolutions vary) goes through the strict path in dinov3_tagger.
        """
        for kwargs in ({"do_resize": False, "do_center_crop": False},
                       {"do_resize": False},
                       {}):
            try:
                return self._processor(images=batch, return_tensors="pt", **kwargs)
            except (TypeError, ValueError):
                continue
        raise RuntimeError("image processor rejected every call form")

    def embed(self, images: list, batch_size: int = 32):
        import torch

        chunks = []
        for i in range(0, len(images), batch_size):
            batch = [
                dinov3_tagger.apply_preprocessing(img, self.preprocessing,
                                                  self.resolution)
                for img in images[i:i + batch_size]
            ]
            inp = self._process(batch)
            pixel_values = inp["pixel_values"]
            if self._torch_dtype is not None:
                pixel_values = pixel_values.to(self._torch_dtype)
            if self._device != "cpu":
                pixel_values = pixel_values.to(self._device)
            with torch.no_grad():
                out = self._model(pixel_values=pixel_values)
            # Cast to fp32 BEFORE normalizing: under bf16 the norm is computed at ~3
            # decimal digits and the stored "unit" vector would not be unit.
            feats = self._pool(out).float()
            feats = feats / feats.norm(dim=-1, keepdim=True)
            chunks.append(feats.cpu())
        return torch.cat(chunks) if chunks else None


def load_encoder(arm: dict[str, Any], *, revision: str, device: str):
    """The right loader for this arm's family, pinned to `revision`."""
    if arm["pooling"] in dinov3_tagger.POOLING_MODES:
        return dinov3_tagger.Dinov3Tagger.load(
            config={
                "model": arm["model"], "revision": revision,
                "library": arm["library"], "pooling": arm["pooling"],
                "resolution": int(arm["resolution"]),
                "preprocessing": arm["preprocessing"], "dtype": arm["dtype"],
            },
            device=device,
        )
    return SimpleEncoder.load(
        model_id=arm["model"], revision=revision,
        model_class=_model_class_for(arm["pooling"]), pooling=arm["pooling"],
        resolution=int(arm["resolution"]), preprocessing=arm["preprocessing"],
        dtype=arm["dtype"], device=device,
    )


def _model_class_for(pooling: str) -> str:
    """The transformers class whose forward pass produces this pooling's tensor.

    Derived from pooling rather than stored on the arm row, because the arms table
    carries the vector's identity and a class name is not part of it: two classes that
    produce the same tensor produce the same population.
    """
    if pooling == "image_embeds":
        # The PROJECTION head is the half that produces image_embeds; the bare vision
        # model has no projection, and CLIPModel would demand input_ids we have no text
        # for.
        return "CLIPVisionModelWithProjection"
    if pooling == "attention_pool":
        return "SiglipVisionModel"
    return "AutoModel"


def fetch_manifest(*, manifest_key: str) -> dict[str, Any]:
    r2 = image_storage.R2Client.from_env()
    return json.loads(r2.download_bytes(manifest_key).decode("utf-8"))


def cache_images(manifest: dict[str, Any], *, cache_dir: str, workers: int,
                 get: Any = None,
                 on_progress: Callable[[int, int], None] | None = None) -> dict[int, str]:
    """{image_id: local path}, downloaded once and reused by every arm. Resumable — a
    file already on disk is not re-fetched, so a restarted pod pays only for what it
    had not finished."""
    if get is None:
        import requests

        get = requests.Session().get
    os.makedirs(cache_dir, exist_ok=True)

    def _one(item: tuple[str, dict[str, Any]]) -> tuple[int, str | None]:
        image_id, meta = int(item[0]), item[1]
        path = os.path.join(cache_dir, f"{image_id}.img")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return image_id, path
        for attempt in range(3):
            try:
                resp = get(meta["url"], timeout=60)
                resp.raise_for_status()
                with open(path, "wb") as fh:
                    fh.write(resp.content)
                return image_id, path
            except Exception:  # noqa: BLE001 - retry, then drop this one image
                time.sleep(1.0 * (attempt + 1))
        return image_id, None

    items = manifest.get("images", {})
    out: dict[int, str] = {}
    failed = 0
    seen = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for image_id, path in pool.map(_one, items.items()):
            seen += 1
            if path is None:
                failed += 1
            else:
                out[image_id] = path
            # The pod writes no vector during this phase; without a heartbeat the
            # dispatcher's stall deadline would tear down a pod that is working.
            if on_progress is not None and seen % CACHE_HEARTBEAT_EVERY == 0:
                on_progress(seen, len(items))
    LOG.info("CACHE ready images=%d failed=%d dir=%s", len(out), failed, cache_dir)
    return out


def _decode(paths: Sequence[tuple[int, str]]) -> list[tuple[int, Any]]:
    from PIL import Image

    decoded: list[tuple[int, Any]] = []
    for image_id, path in paths:
        try:
            with Image.open(path) as img:
                decoded.append((image_id, img.convert("RGB")))
        except Exception:  # noqa: BLE001 - stored bytes that will never decode
            LOG.debug("undecodable image_id=%d path=%s", image_id, path)
    return decoded


def _vec_str(row) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in row.tolist()) + "]"


def done_image_ids(conn: Any, *, arm_id: int) -> set[int]:
    with conn.cursor() as cur:
        cur.execute(_DONE_IDS_SQL, {"arm_id": arm_id})
        return {int(r[0]) for r in cur.fetchall()}


def read_run(conn: Any, *, run_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_RUN_SQL, {"run_id": run_id})
        row = cur.fetchone()
    if row is None:
        return None
    return dict(zip(("id", "label", "status", "manifest_key"), row))


def read_arms(conn: Any, *, run_id: int) -> list[dict[str, Any]]:
    keys = ("id", "arm", "model", "revision", "library", "pooling", "resolution",
            "preprocessing", "dtype", "status")
    with conn.cursor() as cur:
        cur.execute(_ARMS_SQL, {"run_id": run_id})
        return [dict(zip(keys, row)) for row in cur.fetchall()]


def pending_arms(arms: Sequence[dict[str, Any]], *,
                 only: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """Arms this pass should work on.

    Never the zero-GPU stored arm — the manifest stage already filled that one in, and a
    pod has nothing to compute for it. Otherwise: unfinished arms by default, but naming
    an arm explicitly in `--arms` OVERRIDES its status, which is how a `skipped` arm gets
    retried once the operator fixes the token or accepts the licence. (Work already done
    is still skipped image-by-image, so a forced re-run of a finished arm is a no-op
    rather than a duplicate.)

    `failed` IS RETRYABLE HERE AND TERMINAL THERE. `scripts/pod_watchdog.py`'s
    all-terminal case counts `ok`/`failed`/`skipped` as terminal and tears the pod down,
    which is right — for THIS launch. The two rules only agree because
    `tagging_bakeoff_dispatch.reset_failed_arms` clears the previous attempt's `failed`
    arms back to `pending` before the pod starts, so a retry is a new attempt rather than
    a run the watchdog considers already over (2026-09-08 (j)). Do not "fix" either end
    by making them match.
    """
    wanted = {a.strip() for a in (only or []) if a.strip()}
    out = []
    for arm in arms:
        if arm["arm"] == arms_mod.STORED_CLIP_ARM:
            continue
        if wanted:
            if arm["arm"] in wanted:
                out.append(arm)
            continue
        if arm["status"] not in ("pending", "running", "failed"):
            continue
        out.append(arm)
    return out


def embed_arm(conn: Any, arm: dict[str, Any], *, paths: dict[int, str], device: str,
              batch_size: int, deadline: float | None, versions: str = "",
              on_heartbeat: Callable[[str], None] | None = None,
              ) -> tuple[str, int, str, int | None]:
    """Embed everything this arm still owes. Returns (status, written, note, dim).

    `dim` is the width of a vector this pass actually produced, or None when it produced
    none — the caller COALESCEs, so an arm that was already complete keeps the dimension
    it was stamped with instead of having it overwritten with a guess.
    """
    token = os.environ.get("HF_TOKEN")
    try:
        revision = arms_mod.hub_sha(arm["model"], token=token)
    except Exception as exc:  # noqa: BLE001 - unresolvable sha = skip, never unpinned
        note = f"revision unresolved: {exc}"
        LOG.warning("ARM %s skipped — %s", arm["arm"], note)
        with conn.cursor() as cur:
            cur.execute(_SKIP_ARM_SQL, {"arm_id": arm["id"], "note": note})
        return "skipped", 0, note, None

    with conn.cursor() as cur:
        cur.execute(_START_ARM_SQL, {"arm_id": arm["id"], "revision": revision})

    todo = sorted(set(paths) - done_image_ids(conn, arm_id=arm["id"]))
    LOG.info("ARM %s revision=%s todo=%d/%d device=%s",
             arm["arm"], revision[:12], len(todo), len(paths), device)
    if not todo:
        return "ok", 0, f"already complete at revision {revision}", None

    encoder = load_encoder(arm, revision=revision, device=device)
    written = 0
    dim: int | None = None
    t0 = time.monotonic()
    for start in range(0, len(todo), WRITE_BATCH):
        if deadline and time.monotonic() >= deadline:
            elapsed = time.monotonic() - t0
            note = (f"time budget reached at {written}/{len(todo)} "
                    f"({written / elapsed if elapsed else 0:.1f} img/s); {versions}")
            LOG.warning("ARM %s %s", arm["arm"], note)
            return "failed", written, note, dim
        chunk = todo[start:start + WRITE_BATCH]
        decoded = _decode([(i, paths[i]) for i in chunk])
        if not decoded:
            continue
        vectors = encoder.embed([img for _, img in decoded], batch_size)
        if vectors is None:
            continue
        dim = int(vectors.shape[1])
        params = [(arm["id"], image_id, _vec_str(vectors[i]))
                  for i, (image_id, _img) in enumerate(decoded)]
        with conn.cursor() as cur:
            cur.executemany(_INSERT_VECTOR_SQL, params)
        written += len(params)
        elapsed = time.monotonic() - t0
        LOG.info("ARM %s progress=%d/%d dim=%d %.1f img/s",
                 arm["arm"], written, len(todo), dim,
                 written / elapsed if elapsed else 0.0)
        # THE HEARTBEAT, written with the vectors it reports: same connection, same
        # batch boundary, so a note that says "3000 vectors" is a note whose 3000
        # vectors are already committed.
        beat = (f"{iso_now()} running {written}/{len(todo)} vectors dim={dim} "
                f"on {device}; {versions}")
        with conn.cursor() as cur:
            cur.execute(_HEARTBEAT_ARM_SQL, {"arm_id": arm["id"], "note": beat[:2000]})
        if on_heartbeat is not None:
            on_heartbeat(f"arm {arm['arm']} {written}/{len(todo)} vectors")

    elapsed = time.monotonic() - t0
    rate = written / elapsed if elapsed else 0.0
    note = (f"{written} vectors at revision {revision}; {rate:.1f} img/s end-to-end "
            f"(decode + forward), {elapsed:.0f}s on {device}; {versions}")
    return "ok", written, note, dim


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-id", type=int, required=True)
    p.add_argument("--arms", default="",
                   help="Comma-separated arm names to restrict this pass to.")
    p.add_argument("--batch-size", type=int, default=32, help="Model forward batch.")
    p.add_argument("--workers", type=int, default=16,
                   help="Parallel image downloads while filling the cache.")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR)
    p.add_argument("--device", default="cuda",
                   help="'cuda' on a pod, 'cpu' anywhere else. Falls back to cpu with "
                        "a warning when torch reports no GPU — a silent CPU run at "
                        "1024px would look like a hang.")
    p.add_argument("--max-seconds", type=float, default=0,
                   help="Time budget; an arm that runs into it stops at a batch "
                        "boundary and is left 'failed' with what it wrote intact "
                        "(re-running resumes). 0 = unbounded.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the run, its arms and what each one still owes. "
                        "Downloads nothing, loads no weights, writes nothing.")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    only = [a.strip() for a in args.arms.split(",") if a.strip()]
    device = args.device
    if device != "cpu" and not args.dry_run:
        import torch

        if not torch.cuda.is_available():
            LOG.warning("--device=%s but torch reports no GPU — falling back to cpu. "
                        "This will be very slow at the larger resolutions.", device)
            device = "cpu"

    import psycopg

    versions = runtime_versions()
    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        run = read_run(conn, run_id=args.run_id)
        if run is None:
            LOG.error("no bake-off run with id=%d", args.run_id)
            return 1
        # THE FIRST DB WRITE, deliberately before the manifest, the cache and any weight
        # download: it is the only thing that tells the dispatcher's watchdog the clone
        # and the install actually worked (scripts/pod_watchdog.py).
        if not args.dry_run:
            stamp_run(conn, run_id=args.run_id, boot=versions, alive="starting")
            LOG.info("BOOT %s", versions)
        arms = read_arms(conn, run_id=args.run_id)
        todo_arms = pending_arms(arms, only=only)
        LOG.info("BAKEOFF run_id=%d label=%r arms=%d pending=%d manifest=%s",
                 run["id"], run["label"], len(arms), len(todo_arms),
                 run["manifest_key"])
        for arm in todo_arms:
            LOG.info("  pending %s (%s @%s/%s %s)", arm["arm"], arm["model"],
                     arm["resolution"], arm["dtype"], arm["preprocessing"])
        if args.dry_run:
            LOG.info("BAKEOFF dry_run — nothing downloaded, loaded or written.")
            return 0
        if not todo_arms:
            LOG.info("BAKEOFF nothing to do.")
            return 0
        if not run["manifest_key"]:
            LOG.error("run %d has no manifest_key — run the manifest stage first",
                      args.run_id)
            return 1

        def alive(message: str) -> None:
            try:
                stamp_run(conn, run_id=args.run_id, alive=message)
            except Exception as exc:  # noqa: BLE001 - a heartbeat must never kill a run
                LOG.warning("heartbeat write failed (continuing): %s", exc)

        alive("fetching manifest")
        manifest = fetch_manifest(manifest_key=run["manifest_key"])
        paths = cache_images(
            manifest, cache_dir=args.cache_dir, workers=args.workers,
            on_progress=lambda done, total: alive(f"caching images {done}/{total}"))
        if not paths:
            LOG.error("no image could be downloaded — presigned URLs expire after "
                      "%ss; re-run the manifest stage if this run is old",
                      manifest.get("expires_in"))
            return 1

        deadline = time.monotonic() + args.max_seconds if args.max_seconds else None
        results: list[tuple[str, str, int]] = []
        for arm in todo_arms:
            try:
                alive(f"arm {arm['arm']} loading")
                status, written, note, dim = embed_arm(
                    conn, arm, paths=paths, device=device,
                    batch_size=args.batch_size, deadline=deadline,
                    versions=versions, on_heartbeat=alive)
            except Exception as exc:  # noqa: BLE001 - one bad arm must not lose the rest
                status, written, note, dim = "failed", 0, f"{type(exc).__name__}: {exc}", None
                LOG.exception("ARM %s failed", arm["arm"])
            if status != "skipped":
                with conn.cursor() as cur:
                    cur.execute(_FINISH_ARM_SQL, {
                        "arm_id": arm["id"], "status": status,
                        "dim": dim, "note": note[:2000],
                    })
            LOG.info("ARM %s %s — %s", arm["arm"], status, note)
            results.append((arm["arm"], status, written))

    ok = sum(1 for _, status, _ in results if status == "ok")
    LOG.info("BAKEOFF done run_id=%d arms_ok=%d/%d", args.run_id, ok, len(results))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
