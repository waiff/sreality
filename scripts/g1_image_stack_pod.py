"""G1 image-stack bake-off: the pod payload (GPU; runs on CPU for a smoke test).

Answers one question on our own photos: does a modern retrieval stack find the same
physical room across two adverts where today's dHash needs an identical frame?

Per image (every image of the manifest, fetched once from R2):
  * SSCD `sscd_disc_mixup` (ResNet-50, 512-d, MIT) — copy detection: crop, resize,
    watermark and re-encode robust;
  * DINOv2 ViT-L/14-reg @504 bf16 (Apache-2.0) — the licence-clean global descriptor;
  * DINOv3 ViT-B/16 @768 bf16 (DINOv3 licence, gated) — the active tag model's own
    encoder, PINNED to the identity stored on that model, so the v1 heads can score it;
  * the active tag model's head scores (kitchen, bathroom, living room, floor plan, ...).
Per frame pair (routed: same CLIP top tag, same head winner, top-k DINOv3, top-k SSCD,
plus the dHash-identical pairs as a positive control):
  * LightGlue matches with SuperPoint (restrictive licence: measurement only) and with
    DISK (Apache-2.0), then MAGSAC fundamental-matrix and homography inliers.
Synthetic copies (P1b of ENCODER-DECISION section 5.2): sampled originals under six
transforms (crop, downscale + JPEG, watermark, letterbox, combo, tone), with the engine's
own dHash of each, embedded by every descriptor arm plus the incumbent CLIP B/32.

Everything lands in one directory, tarred and uploaded to R2 at
`bakeoff/g1-image-stack/<run_id>/results.tar`; the dispatcher's collect step turns it into
a GitHub artifact. IT WRITES NO PRODUCTION TABLE: progress heartbeats go into the
`dedup_sim.tag_head_bakeoff_runs/arms` rows the dispatcher created for this run (the same
rows the tagging lane's watchdog reads), nothing else. It decides no merge.

Every phase is checkpointed by its output file, and every arm IN PROGRESS by its shards
(`<arm>.shards/NNNNNN.npz`, EMBED_SHARD vectors or MATCH_SHARD pairs each, written as they
are produced), so a restarted payload resumes after the last shard; `--max-seconds` stops
at a batch boundary and still uploads. The shards are also the watchdog's progress unit
(`units=` on every heartbeat), never the heartbeat itself.

THE OOM OF 2026-09-27 (run 2, pod udlpld675b3zwp, ~$2.17, nothing uploaded): the decode
pipeline kept EVERY decoded batch alive for the whole arm (its futures list was never
trimmed), so RSS grew by resolution^2 x 4 bytes per image (Pillow keeps RGB at 4 bytes a
pixel) until the cgroup killed the payload (exit 137) at DINOv3 image 34,848 of 98,578;
and because an arm was written only at its end, every restart began DINOv3 from zero and
died again, ten passes in a row. Now the queue is bounded both ways (decoded_batches),
the vectors go to disk per shard, the queue is sized from the pod's RAM (plan_decode) and
the bootstrap gives a failing payload up after at most two restarts (pod_bootstrap).

Usage (pod):   python -m scripts.g1_image_stack_pod --run-id 12 --device cuda
Usage (local): python -m scripts.g1_image_stack_pod --no-db --local-manifest m.json.gz \\
                   --image-dir imgs/ --out /tmp/g1 --device cpu --smoke
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tarfile
import time
from collections import OrderedDict, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Iterator, Sequence

LOG = logging.getLogger("g1_image_stack_pod")

RESULTS_PREFIX = "bakeoff/g1-image-stack"
DEFAULT_ROOT = "/opt/podboot/g1"

SSCD_URL = "https://dl.fbaipublicfiles.com/sscd-copy-detection/sscd_disc_mixup.torchscript.pt"
SSCD_SIZE = 320
LIGHTGLUE_GIT = "git+https://github.com/cvg/LightGlue.git@{sha}"
LIGHTGLUE_SHA = "eb42fee2d71449efb0aa5c10549752b5d75384d8"   # cvg/LightGlue main, 2026-02-18
EXTRA_DEPS = ("opencv-python-headless>=4.9", "kornia>=0.7")

DINOV2_ARM = {"arm": "dinov2-l14-reg@504/bf16", "model": "facebook/dinov2-with-registers-large",
              "library": "transformers", "pooling": "cls", "resolution": 504,
              "preprocessing": "letterbox_pad", "dtype": "bf16"}
DINOV3_ARM = {"arm": "dinov3-b16@768/bf16", "model": "facebook/dinov3-vitb16-pretrain-lvd1689m",
              "library": "transformers", "pooling": "cls", "resolution": 768,
              "preprocessing": "letterbox_pad", "dtype": "bf16"}
CLIP_ARM = {"arm": "clip-b32-openai@224/fp32", "model": "openai/clip-vit-base-patch32",
            "library": "transformers (scraper.clip_tagger)", "pooling": "image_embeds",
            "resolution": 224, "preprocessing": "clip_processor", "dtype": "fp32"}

MAX_KEYPOINTS = 1024
MATCH_RESIZE = 1024
STOCK_POP = 8
ROUTE_CAP = 4                 # frames per head per side
TOPK_DINO = 8
TOPK_SSCD = 4
HEAD_FLOOR = 0.5              # the ledger's consumer floor (new-dedup PROGRAM.md)
SYNTH_N = 1500
SYNTH_GALLERY = 10000
SYNTH_TRANSFORMS = ("crop10", "down50_q60", "watermark", "letterbox", "combo", "tone_q75")
SEED = 20260927
EMBED_SHARD = 4096            # vectors per on-disk shard (~1.5 GPU-min of DINOv3 on a 3090)
MATCH_SHARD = 1000            # LightGlue pairs per on-disk shard
DECODE_RAM_SHARE = 0.10       # of the pod's memory limit, for decoded images awaiting the GPU
PIL_RGB_BYTES = 4             # Pillow stores an RGB image at 4 bytes per pixel
FULL_DECODE_BYTES = 2048 * 1536 * PIL_RGB_BYTES   # an un-resized photo (the CLIP loader)
UNLIMITED = 1 << 60           # cgroup v1 writes "no limit" as a number near 2^63

ROUTE_CLIP, ROUTE_HEAD, ROUTE_DINO, ROUTE_SSCD, ROUTE_DHASH, ROUTE_NB = 1, 2, 4, 8, 16, 32
# LightGlue verifies same-room pairs only in the rooms that identify a unit; hallway,
# facade, garden and balcony frames are still scored by every descriptor, never matched.
LG_ROOMS = frozenset({"kitchen", "bathroom", "living_room", "bedroom", "toilet", "floor_plan"})
NB_LG_ROOMS = frozenset({"kitchen", "bathroom", "floor_plan"})
NB_ANCHOR_FRAMES = 2          # anchor frames per room searched against the neighbourhood
# v1 head tag ids -> the engine's room names (C6 section 2.5; ids per PROGRAM.md:764).
HEAD_ROOM = {25: "kitchen", 22: "bathroom", 28: "living_room", 46: "floor_plan",
             39: "plan_3d", 3: "exterior_facade", 17: "garage", 48: "technical",
             42: "site_plan", 43: "site_plan", 45: "property_document"}
# Routing by the head's registry LABEL, not its id: C6 infers 10 of the 11 id-to-label
# pairings from list order (only 39 = 3d plan is verified), so the ids above are a
# fallback. Order matters only for labels that carry two keys (none today).
HEAD_LABEL_ROOMS = (("kuchy", "kitchen"), ("koupel", "bathroom"), ("obývací", "living_room"),
                    ("obyvaci", "living_room"), ("3d", "plan_3d"), ("půdorys", "floor_plan"),
                    ("pudorys", "floor_plan"), ("fasád", "exterior_facade"),
                    ("fasad", "exterior_facade"), ("garáž", "garage"), ("garaz", "garage"),
                    ("technick", "technical"), ("katastr", "site_plan"), ("letecký", "site_plan"),
                    ("letecky", "site_plan"), ("property", "property_document"))
HARD_CLASSES = frozenset({"neg_rule", "neg_unit", "neg_mnl", "neg_fused", "neg_sib_hard"})


def head_room_map(heads: Sequence[dict[str, Any]]) -> dict[int, str]:
    """tag id -> engine room, read from each head's label; unknown labels keep the id map."""
    out = dict(HEAD_ROOM)
    for h in heads:
        label = (h.get("label") or "").lower()
        for key, room in HEAD_LABEL_ROOMS:
            if key in label:
                out[int(h["tag_id"])] = room
                break
    return out

# SuperPoint's weights are Magic Leap's "academic or non-profit, noncommercial research use
# only" licence: never in the default phases; `--phases ...,match-superpoint` is an
# explicit operator decision. DISK (Apache-2.0) and ALIKED (BSD-3) are the licence-clean pair.
# Cheapest-to-most-expensive after the descriptors, so a deadline cuts the tail: the copy
# test and the licence-clean ALIKED matcher land before DISK.
PHASES = ("embed", "heads", "route", "synthetic", "match-aliked", "match-disk", "upload")
OPTIONAL_PHASES = ("match-superpoint",)
EXTRACTORS = ("aliked", "disk", "superpoint")
ARM_OF_PHASE = {p: f"g1:{p}" for p in PHASES + OPTIONAL_PHASES}

_RUN_SQL = "SELECT id, label, status, manifest_key FROM dedup_sim.tag_head_bakeoff_runs WHERE id = %(run_id)s"
_ARM_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms SET status = %(status)s, note = %(note)s,
           revision = COALESCE(NULLIF(%(revision)s, ''), revision)
    WHERE run_id = %(run_id)s AND arm = %(arm)s
"""
_RUN_NOTE_SQL = "SELECT note FROM dedup_sim.tag_head_bakeoff_runs WHERE id = %(run_id)s"
_WRITE_RUN_NOTE_SQL = "UPDATE dedup_sim.tag_head_bakeoff_runs SET note = %(note)s WHERE id = %(run_id)s"
_FINISH_RUN_SQL = "UPDATE dedup_sim.tag_head_bakeoff_runs SET status = %(status)s WHERE id = %(run_id)s"
_FAIL_OPEN_ARMS_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms SET status = 'failed', note = %(note)s
    WHERE run_id = %(run_id)s AND arm LIKE 'g1:%%' AND status NOT IN ('ok', 'failed', 'skipped')
"""
BOOT_PREFIX = "pod booted "
ALIVE_PREFIX = "pod alive "


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rss_bytes(status: str = "/proc/self/status") -> int:
    """This process's resident memory now (0 where /proc is absent)."""
    try:
        with open(status) as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


class Reporter:
    """Heartbeats into the dispatcher's rows; a no-op without a database (local runs).

    Every `pod alive` line ends in `units=<n> rss=<GB>`: n is `units_fn()`, the durable work
    on local disk (shards and result files), which is what the dispatcher's stall deadline
    compares — a heartbeat that finished nothing is not progress (2026-09-27)."""

    def __init__(self, conn: Any, run_id: int,
                 units_fn: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.run_id = run_id
        self.units_fn = units_fn
        self.last = 0.0
        self.terminal: set[str] = set()

    def progress_tag(self) -> str:
        try:
            units = self.units_fn() if self.units_fn is not None else 0
        except OSError:
            units = 0
        return f"units={units} rss={rss_bytes() / 2**30:.1f}GB"

    def run_note(self, *, boot: str | None = None, alive: str | None = None) -> None:
        if alive is not None:
            alive = f"{alive} {self.progress_tag()}"
        if self.conn is None:
            LOG.info("NOTE boot=%s alive=%s", boot, alive)
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute(_RUN_NOTE_SQL, {"run_id": self.run_id})
                row = cur.fetchone()
            lines = [ln for ln in ((row[0] if row else "") or "").splitlines()
                     if not ln.startswith(ALIVE_PREFIX)
                     and not (boot is not None and ln.startswith(BOOT_PREFIX))]
            if boot is not None:
                lines.append(f"{BOOT_PREFIX}{iso_now()} {boot}")
            if alive is not None:
                lines.append(f"{ALIVE_PREFIX}{iso_now()} {alive}")
            with self.conn.cursor() as cur:
                cur.execute(_WRITE_RUN_NOTE_SQL, {"run_id": self.run_id,
                                                  "note": "\n".join(lines)[-6000:]})
        except Exception as exc:  # noqa: BLE001 - a heartbeat never kills the run
            LOG.warning("run note write failed: %s", exc)

    def phase(self, phase: str, status: str, note: str, revision: str = "") -> None:
        LOG.info("PHASE %s %s %s", phase, status, note)
        self.last = time.monotonic()
        if status in ("ok", "failed", "skipped"):
            self.terminal.add(phase)
        if self.conn is None:
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute(_ARM_SQL, {"run_id": self.run_id, "arm": ARM_OF_PHASE[phase],
                                       "status": status, "revision": revision,
                                       "note": f"{iso_now()} {note}"[:2000]})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("arm note write failed: %s", exc)

    def beat(self, phase: str, note: str, every_s: float = 60.0) -> None:
        if time.monotonic() - self.last >= every_s:
            self.phase(phase, "running", note)
            # The run note carries the units the watchdog reads; the arm note alone would not.
            self.run_note(alive=f"{phase} {note}")

    def finish_run(self, status: str) -> None:
        if self.conn is None:
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute(_FINISH_RUN_SQL, {"run_id": self.run_id, "status": status})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("run status write failed: %s", exc)

    def fail_open_arms(self, note: str) -> None:
        """Every g1 arm not yet terminal becomes `failed` — the all-terminal cue."""
        if self.conn is None:
            LOG.info("ARMS failed: %s", note)
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute(_FAIL_OPEN_ARMS_SQL, {"run_id": self.run_id,
                                                  "note": f"{iso_now()} {note}"[:2000]})
        except Exception as exc:  # noqa: BLE001
            LOG.warning("arm close write failed: %s", exc)


def runtime_versions() -> str:
    parts = [f"py{sys.version.split()[0]}"]
    for name in ("torch", "transformers", "lightglue", "cv2", "kornia"):
        try:
            module = __import__(name)
            parts.append(f"{name}{getattr(module, '__version__', '?')}")
        except Exception:  # noqa: BLE001
            parts.append(f"{name}=absent")
    return " ".join(parts)


def ensure_deps(lightglue_sha: str) -> None:
    """LightGlue is not on PyPI and pins `opencv-python` (needs libGL a bare pod lacks),
    so it goes in with --no-deps beside the headless OpenCV. Idempotent."""
    try:
        import cv2  # noqa: F401
        import kornia  # noqa: F401
        import lightglue  # noqa: F401
        return
    except Exception:  # noqa: BLE001
        pass
    base = ["uv", "pip", "install", "--quiet"]
    subprocess.run(base + list(EXTRA_DEPS), check=True)
    subprocess.run(base + ["--no-deps", LIGHTGLUE_GIT.format(sha=lightglue_sha)], check=True)


# ---------------------------------------------------------------------------------------
# Manifest and images
# ---------------------------------------------------------------------------------------

def load_manifest(*, local: str | None, key: str | None) -> dict[str, Any]:
    if local:
        with gzip.open(local, "rt") as fh:
            return json.load(fh)
    from scraper import image_storage

    raw = image_storage.R2Client.from_env().download_bytes(key)
    return json.loads(gzip.decompress(raw).decode("utf-8"))


def cache_images(images: Sequence[dict[str, Any]], *, cache_dir: str, image_dir: str | None,
                 workers: int, beat: Callable[[str], None],
                 on_count: Callable[[int], None] | None = None) -> dict[int, str]:
    """{image_id: local path}. Resumable: a file on disk is not fetched again, and a key
    that failed all three attempts in an earlier pass on this disk (`failed.json`) is not
    retried — on 2026-09-27 each restart re-paid 458 dead keys x 3 attempts x 6 s."""
    os.makedirs(cache_dir, exist_ok=True)
    if image_dir:
        out = {}
        for r in images:
            for ext in (".jpg", ".jpeg", ".png", ".img"):
                p = os.path.join(image_dir, f"{r['image_id']}{ext}")
                if os.path.exists(p):
                    out[int(r["image_id"])] = p
                    break
        if on_count is not None:
            on_count(len(out))
        return out
    from scraper import image_storage

    r2 = image_storage.R2Client.from_env(max_pool_connections=max(32, workers))
    failed_path = os.path.join(cache_dir, "failed.json")
    try:
        with open(failed_path) as fh:
            known_dead = {int(i) for i in json.load(fh)}
    except (OSError, ValueError, TypeError):
        known_dead = set()

    def one(r: dict[str, Any]) -> tuple[int, str | None]:
        iid = int(r["image_id"])
        path = os.path.join(cache_dir, f"{iid}.img")
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return iid, path
        if not r.get("key") or iid in known_dead:
            return iid, None
        for attempt in range(3):
            try:
                data = r2.download_bytes(r["key"])
                tmp = path + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
                return iid, path
            except Exception:  # noqa: BLE001
                time.sleep(1.0 + attempt)
        return iid, None

    out: dict[int, str] = {}
    dead: list[int] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (iid, path) in enumerate(pool.map(one, images), 1):
            if path:
                out[iid] = path
            else:
                dead.append(iid)
            if n % 2000 == 0:
                if on_count is not None:
                    on_count(len(out))
                beat(f"images {n}/{len(images)} failed={len(dead)}")
    try:
        with open(failed_path + ".part", "w") as fh:
            json.dump(sorted(dead), fh)
        os.replace(failed_path + ".part", failed_path)
    except OSError:
        pass
    if on_count is not None:
        on_count(len(out))
    LOG.info("CACHE images=%d failed=%d", len(out), len(dead))
    return out


def decode(path: str):
    from PIL import Image

    with Image.open(path) as img:
        return img.convert("RGB")


def preprocessed_loader(preprocessing: str, resolution: int) -> Callable[[str], Any]:
    """Decode AND the arm's own geometry, in the decode threads. Every transform is the
    identity on an image already at its resolution square (Pillow short-circuits a
    same-size resize), so the encoder's own call is a copy and the vector is bit-identical
    to the production arm's, while the GPU no longer waits on serial resizing."""
    from scraper import dinov3_tagger

    def load(path: str):
        return dinov3_tagger.apply_preprocessing(decode(path), preprocessing, resolution)

    return load


def decoded_batches(items: Sequence[tuple[Any, Any]], batch: int, workers: int,
                    loader: Callable[[Any], Any],
                    prefetch: int = 2) -> Iterator[tuple[list[Any], list[Any]]]:
    """(ids, PIL images) in batches, each decoded in one pool thread, at most `prefetch`
    batches ahead of the consumer.

    BOUNDED BOTH WAYS. A batch is submitted only when the consumer takes one (the decode
    pool waits while the GPU is behind), and a batch the consumer has taken is dropped from
    the queue. The 2026-09-27 version appended every future to a list it never trimmed, and
    a finished future holds its result: the whole arm's decoded images stayed alive, about
    82 GB at DINOv3@768 image 34,848, and the cgroup killed the payload."""
    def load(chunk):
        ids, imgs = [], []
        for key, src in chunk:
            try:
                imgs.append(loader(src))
                ids.append(key)
            except Exception:  # noqa: BLE001 - bytes that will never decode
                LOG.debug("undecodable %s", key)
        return ids, imgs

    chunks = (items[i:i + batch] for i in range(0, len(items), batch))
    depth = max(1, prefetch)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, depth))) as pool:
        queue: deque[Any] = deque(pool.submit(load, c) for _, c in zip(range(depth), chunks))
        while queue:
            head = queue.popleft()
            nxt = next(chunks, None)
            if nxt is not None:
                queue.append(pool.submit(load, nxt))
            result = head.result()
            del head
            yield result


# ---------------------------------------------------------------------------------------
# Descriptors
# ---------------------------------------------------------------------------------------

class SSCD:
    def __init__(self, path: str, device: str) -> None:
        import torch

        self.torch = torch
        self.device = device
        self.model = torch.jit.load(path, map_location=device).eval()
        with open(path, "rb") as fh:
            self.revision = "sha256:" + hashlib.sha256(fh.read()).hexdigest()

    def embed(self, images: list, batch_size: int = 64):
        import numpy as np
        torch = self.torch
        mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1)
        std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1)
        arrs = [np.asarray(img.resize((SSCD_SIZE, SSCD_SIZE)), dtype=np.float32) / 255.0
                for img in images]
        x = torch.from_numpy(np.stack(arrs)).permute(0, 3, 1, 2)
        x = ((x - mean) / std).to(self.device)
        with torch.no_grad():
            out = self.model(x).float()
        return torch.nn.functional.normalize(out, dim=-1).cpu()


def sscd_loader(path: str):
    """SSCD's own square resize, in the decode threads (its embed call is then a copy)."""
    return decode(path).resize((SSCD_SIZE, SSCD_SIZE))


def fetch_sscd(root: str) -> str:
    path = os.path.join(root, "sscd_disc_mixup.torchscript.pt")
    if not os.path.exists(path):
        import requests

        resp = requests.get(SSCD_URL, timeout=300)
        resp.raise_for_status()
        with open(path + ".part", "wb") as fh:
            fh.write(resp.content)
        os.replace(path + ".part", path)
    return path


def load_hf_encoder(arm: dict[str, Any], revision: str | None, device: str, threads: int = 0):
    """The tagging lane's own loaders (DINO: post-LN CLS, our letterbox geometry)."""
    from scripts import tagging_bakeoff_arms as arms_mod
    from scripts import tagging_bakeoff_embed as embed_mod

    rev = revision or arms_mod.hub_sha(arm["model"], token=os.environ.get("HF_TOKEN"))
    enc = embed_mod.load_encoder(arm, revision=rev, device=device)
    if threads:
        # The loader sets torch's threads to os.cpu_count(): the HOST's count in a pod.
        import torch

        torch.set_num_threads(threads)
    return enc, rev


def load_production_clip(threads: int = 0) -> tuple[Any, str | None]:
    """scraper.clip_tagger's Tagger: the embedder of the stored vectors (CPU; ~20k images)."""
    from scraper import clip_tagger

    tagger = clip_tagger.Tagger.load(threads=threads)
    return tagger, getattr(tagger, "revision", None)


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
    """The vCPUs this pod may use. os.cpu_count() is the HOST's count inside a RunPod
    container (a 4090 pod gets 6 of a 64+ core host), so RunPod's RUNPOD_CPU_COUNT wins,
    then the cgroup quota, then the affinity mask."""
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


def cpu_workers(requested: int, vcpus: int) -> int:
    """Decode and RANSAC pools are CPU-bound: never more threads than vCPUs; 0 = one each."""
    return max(1, min(requested, vcpus) if requested > 0 else vcpus)


def _read_text(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _cgroup_memory_limit(cgroup_root: str, proc_cgroup: str) -> int | None:
    """The tightest memory limit over this process's cgroup and its ancestors (v2
    `memory.max`, read along the path /proc/self/cgroup names: `/` inside a container with
    its own cgroup namespace), else the v1 `memory.limit_in_bytes`; None when unlimited."""
    def limit(path: str) -> int | None:
        raw = _read_text(path)
        return int(raw) if raw and raw.isdigit() and 0 < int(raw) < UNLIMITED else None

    rel = next((ln[3:].strip() for ln in (_read_text(proc_cgroup) or "").splitlines()
                if ln.startswith("0::")), "/")
    found = []
    while True:
        value = limit(os.path.join(cgroup_root, rel.strip("/"), "memory.max"))
        if value:
            found.append(value)
        if rel in ("", "/"):
            break
        rel = os.path.dirname(rel.rstrip("/"))
    if not found:
        value = limit(os.path.join(cgroup_root, "memory", "memory.limit_in_bytes"))
        if value:
            found.append(value)
    return min(found) if found else None


def pod_memory(meminfo: str = "/proc/meminfo", cgroup_root: str = "/sys/fs/cgroup",
               proc_cgroup: str = "/proc/self/cgroup") -> dict[str, int | None]:
    """The RAM this payload may use: MemTotal (the HOST's, inside a container) and the
    container's cgroup limit; `limit` is the smaller. The OOM killer answers to the
    cgroup, not to MemTotal."""
    total = None
    for line in (_read_text(meminfo) or "").splitlines():
        if line.startswith("MemTotal:"):
            try:
                total = int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                pass
            break
    cgroup = _cgroup_memory_limit(cgroup_root, proc_cgroup)
    known = [v for v in (total, cgroup) if v]
    return {"mem_total": total, "cgroup_limit": cgroup, "limit": min(known) if known else None}


def fs_type(path: str, mounts: str = "/proc/mounts") -> str:
    """The filesystem holding `path` (the longest mount prefix); 'tmpfs' means RAM."""
    real = os.path.realpath(path)
    best, kind = "", "unknown"
    for line in (_read_text(mounts) or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        mnt = parts[1]
        inside = real == mnt or real.startswith(mnt.rstrip("/") + "/")
        if inside and len(mnt) >= len(best):
            best, kind = mnt, parts[2]
    return kind


def plan_decode(limit: int | None, workers: int, requested_batch: int,
                image_bytes: int) -> tuple[int, int]:
    """(batch, prefetch) for one embed arm, from the pod's memory limit.

    A decoded image waiting for the GPU costs `image_bytes` (resolution^2 x 4 for the
    square arms), and the queue — the batches decoded ahead plus the one being embedded —
    may hold DECODE_RAM_SHARE of the limit. Within that, one batch in flight per decode
    worker (each batch decodes in one thread), never fewer than one; the batch shrinks
    only when two of them would not fit."""
    budget = int((limit or 8 * 2**30) * DECODE_RAM_SHARE)
    per = max(1, image_bytes)
    batch = max(1, min(requested_batch, budget // (2 * per)))
    prefetch = max(1, min(workers, budget // (batch * per) - 1))
    return batch, prefetch


def shard_dir(out_path: str) -> str:
    return (out_path[:-len(".npz")] if out_path.endswith(".npz") else out_path) + ".shards"


def shard_files(sdir: str) -> list[str]:
    if not os.path.isdir(sdir):
        return []
    return sorted(os.path.join(sdir, f) for f in os.listdir(sdir)
                  if f.endswith(".npz") and ".tmp." not in f)


def _save_npz(path: str, **arrays: Any) -> None:
    """Atomic: a kill mid-write leaves no half file for a resume to trust."""
    import numpy as np

    np.savez(path + ".tmp.npz", **arrays)
    os.replace(path + ".tmp.npz", path)


def embed_shard_keys(sdir: str) -> tuple[set[Any], set[Any]]:
    """(keys embedded, keys that never decoded) across an unfinished arm's shards. A shard
    that will not load is deleted, so its images are simply embedded again."""
    import numpy as np

    done: set[Any] = set()
    bad: set[Any] = set()
    for path in shard_files(sdir):
        try:
            with np.load(path, allow_pickle=False) as z:
                done.update(z["key"].tolist())
                bad.update(z["bad"].tolist())
        except Exception:  # noqa: BLE001 - redo it rather than trust it
            LOG.warning("dropping unreadable shard %s", path)
            os.remove(path)
    return done, bad


def durable_units(out_dir: str, synth_dir: str | None = None) -> int:
    """Finished work on local disk: result files, the shards of unfinished arms and the
    synthetic copies in hundreds. Compared by the dispatcher's watchdog, never read as a
    quantity: it moves only when something lands, so a heartbeat alone is not progress."""
    n = 0
    if os.path.isdir(out_dir):
        for entry in os.scandir(out_dir):
            if entry.is_dir() and entry.name.endswith(".shards"):
                n += len(shard_files(entry.path))
            elif entry.name.endswith((".npz", ".json")) and entry.name != "report.json":
                n += 1
    if synth_dir and os.path.isdir(synth_dir):
        n += sum(1 for _ in os.scandir(synth_dir)) // (100 * len(SYNTH_TRANSFORMS))
    return n


def embed_arm(name: str, encoder: Any, items: Sequence[tuple[Any, Any]], *, batch: int,
              workers: int, loader: Callable[[Any], Any], out_path: str,
              beat: Callable[[str], None], deadline: float | None, prefetch: int = 2,
              shard_size: int = EMBED_SHARD,
              on_shard: Callable[[str], None] | None = None) -> dict[str, Any]:
    """One descriptor (a global vector per image, nothing else) over `items`, written as it
    is produced: every `shard_size` vectors land atomically in `<arm>.shards/`, so RAM holds
    one shard and a restarted pass resumes after the last one instead of from zero. The
    arm's own file appears only once every item was tried; a deadline leaves the shards."""
    import numpy as np

    if os.path.exists(out_path):
        return {"arm": name, "skipped": "exists"}
    sdir = shard_dir(out_path)
    os.makedirs(sdir, exist_ok=True)
    done, bad = embed_shard_keys(sdir)
    todo = [it for it in items if it[0] not in done and it[0] not in bad]
    seq = len(shard_files(sdir))
    ids: list[Any] = []
    vecs: list[Any] = []
    bads: list[Any] = []
    fresh = 0
    cut = False

    def flush() -> None:
        nonlocal seq, ids, vecs, bads
        if not ids and not bads:
            return
        mat = np.concatenate(vecs) if vecs else np.zeros((0, 1), np.float16)
        _save_npz(os.path.join(sdir, f"{seq:06d}.npz"), key=np.array(ids), vec=mat,
                  bad=np.array(bads))
        seq += 1
        ids, vecs, bads = [], [], []
        if on_shard is not None:
            on_shard(f"{name} shard {seq}")

    t0 = time.monotonic()
    cursor = 0
    for bids, imgs in decoded_batches(todo, batch, workers, loader, prefetch=prefetch):
        chunk = todo[cursor:cursor + batch]
        cursor += batch
        if len(bids) < len(chunk):
            got = set(bids)
            bads.extend(k for k, _src in chunk if k not in got)
        if imgs:
            v = encoder.embed(imgs, batch_size=batch) if not isinstance(encoder, SSCD) \
                else encoder.embed(imgs)
            vecs.append(v.numpy().astype(np.float16))
            ids.extend(bids)
            fresh += len(bids)
        del imgs
        if len(ids) >= shard_size:
            flush()
        beat(f"{name} {len(done) + fresh}/{len(items)}")
        if deadline and time.monotonic() > deadline:
            LOG.warning("%s: deadline reached at %d/%d", name, len(done) + fresh, len(items))
            cut = True
            break
    flush()
    dt = time.monotonic() - t0
    stats = {"arm": name, "of": len(items), "resumed": len(done), "fresh": fresh,
             "seconds": round(dt, 1), "img_per_s": round(fresh / dt, 2) if dt else None,
             "batch": batch, "prefetch": prefetch, "rss_gb": round(rss_bytes() / 2**30, 2)}
    if cut:
        return {**stats, "n": len(done) + fresh, "partial": True, "shards": seq}
    keys: list[Any] = []
    mats: list[Any] = []
    for path in shard_files(sdir):
        with np.load(path, allow_pickle=False) as z:
            if len(z["key"]):
                keys.extend(z["key"].tolist())
                mats.append(z["vec"])
    mat = np.concatenate(mats) if mats else np.zeros((0, 1), np.float16)
    _save_npz(out_path, key=np.array(keys), vec=mat)
    shutil.rmtree(sdir, ignore_errors=True)
    return {**stats, "n": len(keys)}


def load_vecs(path: str) -> tuple[Any, Any]:
    import numpy as np

    z = np.load(path, allow_pickle=False)
    return z["key"], z["vec"].astype(np.float32)


# ---------------------------------------------------------------------------------------
# Heads
# ---------------------------------------------------------------------------------------

def read_heads(conn: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    from toolkit import tag_models

    model = tag_models.active_model(conn)
    if model is None:
        return None, []
    heads = tag_models.model_heads(conn, model_id=model.id)
    return model.as_dict(), [{"tag_id": h.tag_id, "label": h.label, "artifact": h.artifact}
                             for h in heads]


def score_heads(vec_ids: Any, vecs: Any, heads: list[dict[str, Any]]) -> dict[str, Any]:
    """P(tag) per head: a dot product and a logistic (toolkit.tag_heads.score_embedding),
    vectorised; centroid heads return a cosine, as there."""
    import numpy as np
    from toolkit import tag_heads

    cols = []
    for h in heads:
        art = h["artifact"]
        if art.get("kind") == tag_heads.ARTIFACT_KIND_CENTROID:
            c = np.asarray(art["centroid"], dtype=np.float32)
            cols.append(vecs @ (c / (np.linalg.norm(c) or 1.0)))
        else:
            w = np.asarray(art["weights"], dtype=np.float32)
            cols.append(1.0 / (1.0 + np.exp(-(vecs @ w + float(art["bias"])))))
    scores = np.stack(cols, axis=1) if cols else np.zeros((len(vec_ids), 0), np.float32)
    win = scores.argmax(axis=1) if cols else np.zeros(len(vec_ids), np.int64)
    tag_ids = np.array([h["tag_id"] for h in heads], dtype=np.int64)
    return {"key": vec_ids, "tag_ids": tag_ids, "scores": scores.astype(np.float16),
            "winner": tag_ids[win] if cols else win,
            "winner_score": scores.max(axis=1) if cols else np.zeros(len(vec_ids))}


# ---------------------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------------------

def build_routes(manifest: dict[str, Any], *, dino: tuple[Any, Any] | None,
                 sscd: tuple[Any, Any] | None, heads: dict[str, Any] | None,
                 topk_dino: int = TOPK_DINO, topk_sscd: int = TOPK_SSCD,
                 room_map: dict[int, str] | None = None) -> dict[tuple[int, int], int]:
    """Frame pairs to verify geometrically, each with the bitmask of routes that chose it."""
    import numpy as np

    routes: dict[tuple[int, int], int] = defaultdict(int)

    def put(x: int, y: int, bit: int) -> None:
        a, b = (x, y) if x < y else (y, x)
        routes[(a, b)] |= bit

    for x, y, _tag in manifest.get("frame_pairs", []):
        put(int(x), int(y), ROUTE_CLIP)
    by_listing: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in manifest["images"]:
        by_listing[int(r["listing_id"])].append(r)
    for lst in by_listing.values():
        lst.sort(key=lambda r: (r.get("seq") or 0, r["image_id"]))

    def index(pack):
        if pack is None:
            return None
        ids, mat = pack
        return {int(k): i for i, k in enumerate(ids)}, mat

    di, si = index(dino), index(sscd)
    winner: dict[int, int] = {}
    if heads is not None:
        for k, w, s in zip(heads["key"], heads["winner"], heads["winner_score"]):
            if float(s) >= HEAD_FLOOR:
                winner[int(k)] = int(w)

    def topk(idx, a_imgs, b_imgs, k, bit):
        if idx is None or k <= 0:
            return
        pos, mat = idx
        ia = [r["image_id"] for r in a_imgs if r["image_id"] in pos]
        ib = [r["image_id"] for r in b_imgs if r["image_id"] in pos]
        if not ia or not ib:
            return
        sims = mat[[pos[i] for i in ia]] @ mat[[pos[i] for i in ib]].T
        flat = np.argsort(-sims, axis=None)[:k]
        for f in flat:
            r, c = divmod(int(f), len(ib))
            put(ia[r], ib[c], bit)

    for p in manifest["pairs"]:
        a_imgs, b_imgs = by_listing.get(int(p["a"]), []), by_listing.get(int(p["b"]), [])
        for x, y, _d in p.get("tight", []):
            put(int(x), int(y), ROUTE_DHASH)
        if winner:
            ga: dict[int, list[int]] = defaultdict(list)
            gb: dict[int, list[int]] = defaultdict(list)
            for r in a_imgs:
                w = winner.get(r["image_id"])
                if w is not None and len(ga[w]) < ROUTE_CAP:
                    ga[w].append(r["image_id"])
            for r in b_imgs:
                w = winner.get(r["image_id"])
                if w is not None and len(gb[w]) < ROUTE_CAP:
                    gb[w].append(r["image_id"])
            for w in set(ga) & set(gb):
                for x in ga[w]:
                    for y in gb[w]:
                        put(x, y, ROUTE_HEAD)
        topk(di, a_imgs, b_imgs, topk_dino, ROUTE_DINO)
        topk(si, a_imgs, b_imgs, topk_sscd, ROUTE_SSCD)

    # The catalogue neighbourhood: each anchor frame in a unit room, against the most
    # similar same-room frame of the anchor's stated-different co-live siblings (by SSCD
    # and by DINO), so LightGlue can say whether a sibling carries the same scene.
    room = rooms_of(manifest, heads, room_map)
    for key, sibs in (manifest.get("neighbours") or {}).items():
        anchor_imgs = by_listing.get(int(key), [])
        nb_frames: dict[str, list[int]] = defaultdict(list)
        for _sib, _facts, frame_ids in sibs:
            for iid in frame_ids:
                r = room.get(int(iid))
                if r in NB_LG_ROOMS:
                    nb_frames[r].append(int(iid))
        taken: dict[str, int] = defaultdict(int)
        for img in anchor_imgs:
            r = room.get(int(img["image_id"]))
            if r not in nb_frames or taken[r] >= NB_ANCHOR_FRAMES:
                continue
            taken[r] += 1
            for idx in (di, si):
                if idx is None:
                    continue
                pos, mat = idx
                x = int(img["image_id"])
                cands = [c for c in nb_frames[r] if c in pos]
                if x not in pos or not cands:
                    continue
                sims = mat[[pos[c] for c in cands]] @ mat[pos[x]]
                put(x, cands[int(np.argmax(sims))], ROUTE_NB)
    return dict(routes)


def rooms_of(manifest: dict[str, Any], heads: dict[str, Any] | None,
             room_map: dict[int, str] | None = None) -> dict[int, str]:
    """One room per image: the active head's winner at the consumer floor, else the CLIP
    top tag the export carries (the heads have no bedroom, toilet or hallway)."""
    room_map = room_map or HEAD_ROOM
    out = {int(r["image_id"]): r["tag"] for r in manifest["images"] if r.get("tag")}
    if heads is not None:
        for k, w, s in zip(heads["key"], heads["winner"], heads["winner_score"]):
            if float(s) >= HEAD_FLOOR and int(w) in room_map:
                out[int(k)] = room_map[int(w)]
    return out


def lg_plan(routes: dict[tuple[int, int], int], manifest: dict[str, Any],
            room: dict[int, str]) -> list[tuple[int, int]]:
    """The frame pairs LightGlue verifies, most informative first, cache-friendly within.

    Tier 0: pairs serving a hard negative or a positive whose galleries share at most 3
    non-stock identical frames (h0/hs/h1: the population the stack exists for). Tier 1:
    the catalogue neighbourhood. Tier 2: the rest (K-C-grade positives, random
    siblings). A deadline therefore cuts the least informative tail."""
    listing_of = {int(r["image_id"]): int(r["listing_id"]) for r in manifest["images"]}
    tier_of: dict[tuple[int, int], int] = {}
    for p in manifest["pairs"]:
        cs = set(p.get("classes") or [])
        hard = bool(cs & HARD_CLASSES)
        interesting = bool(cs & {"pos_rule", "pos_merge"}) and p.get("stratum") in ("h0", "hs", "h1")
        key = (min(p["a"], p["b"]), max(p["a"], p["b"]))
        tier_of[key] = min(tier_of.get(key, 2), 0 if hard or interesting else 2)
    keep = []
    for (x, y), bits in routes.items():
        rx, ry = room.get(x), room.get(y)
        same_unit_room = rx is not None and rx == ry and rx in LG_ROOMS
        if not (bits & (ROUTE_DINO | ROUTE_SSCD | ROUTE_DHASH | ROUTE_NB) or same_unit_room):
            continue
        lx, ly = listing_of.get(x, 0), listing_of.get(y, 0)
        tier = tier_of.get((min(lx, ly), max(lx, ly)))
        if tier is None:
            tier = 1 if bits & ROUTE_NB else 2
        keep.append((tier, min(lx, ly), max(lx, ly), x, y))
    keep.sort()
    return [(x, y) for _t, _a, _b, x, y in keep]


# ---------------------------------------------------------------------------------------
# LightGlue
# ---------------------------------------------------------------------------------------

class FeatureCache:
    def __init__(self, extract: Callable[[int], Any], capacity: int = 3000) -> None:
        self.extract = extract
        self.capacity = capacity
        self.data: OrderedDict[int, Any] = OrderedDict()
        self.misses = 0

    def get(self, key: int) -> Any:
        if key in self.data:
            self.data.move_to_end(key)
            return self.data[key]
        self.misses += 1
        val = self.extract(key)
        self.data[key] = val
        if len(self.data) > self.capacity:
            self.data.popitem(last=False)
        return val


def ransac_counts(p0: Any, p1: Any, scale: float) -> tuple[int, int]:
    import cv2
    import numpy as np

    n = len(p0)
    f_in = h_in = 0
    if n >= 8:
        try:
            _, mask = cv2.findFundamentalMat(p0, p1, cv2.USAC_MAGSAC, 1.5 * scale, 0.999, 10000)
            f_in = int(mask.sum()) if mask is not None else 0
        except cv2.error:
            f_in = 0
    if n >= 4:
        try:
            _, mask = cv2.findHomography(p0, p1, cv2.USAC_MAGSAC, 4.0 * scale, maxIters=10000,
                                         confidence=0.999)
            h_in = int(mask.sum()) if mask is not None else 0
        except cv2.error:
            h_in = 0
    return f_in, h_in


def match_phase(extractor_name: str, pairs: Sequence[tuple[int, int]], paths: dict[int, str], *,
                device: str, out_path: str, beat: Callable[[str], None],
                deadline: float | None, workers: int,
                on_shard: Callable[[str], None] | None = None) -> dict[str, Any]:
    """LightGlue over the planned frame pairs. It needs nothing from the embed arms but the
    plan: it extracts its own keypoints from the cached JPEGs (FeatureCache, on the device)
    and keeps only nine numbers per pair, written MATCH_SHARD pairs at a time to
    `<phase>.shards/` so a restart resumes after the last shard."""
    import numpy as np
    import torch
    from lightglue import ALIKED, DISK, LightGlue, SuperPoint
    from lightglue.utils import rbd

    if os.path.exists(out_path):
        return {"extractor": extractor_name, "skipped": "exists"}
    torch.set_grad_enabled(False)
    cls = {"superpoint": SuperPoint, "disk": DISK, "aliked": ALIKED}[extractor_name]
    extractor = cls(max_num_keypoints=MAX_KEYPOINTS).eval().to(device)
    matcher = LightGlue(features=extractor_name).eval().to(device)

    def extract(iid: int):
        img = decode(paths[iid])
        arr = np.asarray(img, dtype=np.float32) / 255.0
        t = torch.from_numpy(arr).permute(2, 0, 1).to(device)
        feats = extractor.extract(t, resize=MATCH_RESIZE)
        size = max(img.size)
        return {k: v for k, v in feats.items()}, size

    cache = FeatureCache(extract)
    sdir = shard_dir(out_path)
    os.makedirs(sdir, exist_ok=True)
    legacy = partial_of(out_path)
    if os.path.exists(legacy):
        # A partial file from a pod that predates the shards is simply one more shard.
        os.replace(legacy, os.path.join(sdir, "legacy.npz"))
    done = {(int(r[0]), int(r[1])) for path in shard_files(sdir) for r in load_match_rows(path)}
    resumed = len(done)
    seq = len(shard_files(sdir))
    fresh: list[tuple[Any, ...]] = []
    written = 0

    def flush() -> None:
        nonlocal seq, fresh, written
        if not fresh:
            return
        save_match_rows(os.path.join(sdir, f"{seq:06d}.npz"), fresh)
        seq += 1
        written += len(fresh)
        fresh = []
        if on_shard is not None:
            on_shard(f"{extractor_name} {resumed + written}/{len(pairs)}")

    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    pending = []
    cut = False
    for n, (x, y) in enumerate(pairs, 1):
        if x not in paths or y not in paths or (x, y) in done:
            continue
        try:
            (fa, sa), (fb, sb) = cache.get(x), cache.get(y)
            out = matcher({"image0": fa, "image1": fb})
            f0, f1, m = rbd(fa), rbd(fb), rbd(out)
            mt = m["matches"]
            sc = m["scores"]
            p0 = f0["keypoints"][mt[:, 0]].cpu().numpy().astype(np.float64)
            p1 = f1["keypoints"][mt[:, 1]].cpu().numpy().astype(np.float64)
            n_hi = int((sc > 0.9).sum().item()) if len(sc) else 0
            meta = (x, y, int(f0["keypoints"].shape[0]), int(f1["keypoints"].shape[0]),
                    int(mt.shape[0]), n_hi, float(sc.mean().item()) if len(sc) else 0.0)
            scale = max(sa, sb) / float(MATCH_RESIZE)
            pending.append((meta, pool.submit(ransac_counts, p0, p1, max(scale, 1.0))))
        except Exception as exc:  # noqa: BLE001 - one bad pair must not lose the phase
            LOG.debug("match failed %s-%s: %s", x, y, exc)
        if len(pending) >= 256:
            fresh.extend(meta + r.result() for meta, r in pending)
            pending = []
        if len(fresh) >= MATCH_SHARD:
            flush()
        if n % 500 == 0:
            beat(f"{extractor_name} {n}/{len(pairs)} cache_misses={cache.misses}")
            if deadline and time.monotonic() > deadline:
                LOG.warning("%s: deadline at %d/%d", extractor_name, n, len(pairs))
                cut = True
                break
    fresh.extend(meta + r.result() for meta, r in pending)
    pool.shutdown()
    flush()
    dt = time.monotonic() - t0
    stats = {"extractor": extractor_name, "of": len(pairs), "resumed": resumed,
             "partial": cut, "seconds": round(dt, 1),
             "pairs_per_s": round(written / dt, 2) if dt else None,
             "feature_extractions": cache.misses}
    if cut:
        # A deadline leaves the shards; a re-dispatch of the same run continues them.
        return {**stats, "pairs": resumed + written, "shards": seq}
    rows = [r for path in shard_files(sdir) for r in load_match_rows(path)]
    save_match_rows(out_path, rows)
    shutil.rmtree(sdir, ignore_errors=True)
    return {**stats, "pairs": len(rows)}


MATCH_COLUMNS = ("a", "b", "kp_a", "kp_b", "matches", "matches_09", "mean_score", "f_inliers",
                 "h_inliers")
CHECKPOINT_S = 1800


def partial_of(out_path: str) -> str:
    return out_path[:-len(".npz")] + ".partial.npz" if out_path.endswith(".npz") else out_path + ".partial"


def save_match_rows(path: str, rows: Sequence[Sequence[Any]]) -> None:
    import numpy as np

    cols = list(zip(*rows)) if rows else [[] for _ in MATCH_COLUMNS]
    arrays = {k: np.asarray(c, dtype=np.float32 if k == "mean_score" else np.int64)
              for k, c in zip(MATCH_COLUMNS, cols)}
    np.savez(path + ".tmp.npz", **arrays)
    os.replace(path + ".tmp.npz", path)


def load_match_rows(path: str) -> list[tuple[Any, ...]]:
    import numpy as np

    z = np.load(path)
    cols = [z[k].tolist() for k in MATCH_COLUMNS]
    return [tuple(r) for r in zip(*cols)]


# ---------------------------------------------------------------------------------------
# Synthetic copies
# ---------------------------------------------------------------------------------------

def transform(img, name: str, rng: random.Random):
    from PIL import Image, ImageDraw, ImageEnhance

    w, h = img.size

    def jpeg(im, q):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q)
        buf.seek(0)
        return Image.open(buf).convert("RGB")

    def crop(im, frac):
        dx, dy = int(w * frac * rng.random()), int(h * frac * rng.random())
        return im.crop((dx, dy, dx + int(w * (1 - frac)), dy + int(h * (1 - frac))))

    def watermark(im):
        im = im.copy()
        d = ImageDraw.Draw(im, "RGBA")
        band = max(12, h // 10)
        y0 = rng.choice([0, h - band])
        d.rectangle((0, y0, w, y0 + band), fill=(255, 255, 255, 110))
        d.text((w // 20, y0 + band // 4), "REALITY  +420 777 000 000", fill=(20, 20, 20, 220))
        s = max(24, w // 8)
        d.rectangle((w - s - 8, 8, w - 8, 8 + s // 2), fill=(200, 30, 30, 160))
        return im

    if name == "crop10":
        return crop(img, 0.10)
    if name == "down50_q60":
        return jpeg(img.resize((max(1, w // 2), max(1, h // 2))), 60)
    if name == "watermark":
        return jpeg(watermark(img), 85)
    if name == "letterbox":
        pad = int(h * 0.12)
        canvas = Image.new("RGB", (w, h + 2 * pad), (0, 0, 0))
        canvas.paste(img, (0, pad))
        return jpeg(canvas.resize((w, h)), 85)
    if name == "combo":
        return jpeg(watermark(crop(img, 0.08)), 50)
    if name == "tone_q75":
        im = ImageEnhance.Brightness(img).enhance(1.15)
        return jpeg(ImageEnhance.Contrast(im).enhance(1.15), 75)
    raise ValueError(name)


def synthetic_plan(manifest: dict[str, Any], paths: dict[int, str], n: int, gallery: int) -> dict[str, Any]:
    rng = random.Random(SEED)
    cand = [r for r in manifest["images"] if int(r["image_id"]) in paths
            and (r.get("pop") is None or r["pop"] < STOCK_POP)]
    rng.shuffle(cand)
    plans = [r for r in cand if r.get("tag") == "floor_plan"][: n // 5]
    rest = [r for r in cand if r.get("tag") != "floor_plan"][: n - len(plans)]
    sources = plans + rest
    src_ids = {int(r["image_id"]) for r in sources}
    distract = [int(r["image_id"]) for r in cand if int(r["image_id"]) not in src_ids][:gallery]
    return {"sources": [int(r["image_id"]) for r in sources],
            "source_tag": {int(r["image_id"]): r.get("tag") for r in sources},
            "gallery": sorted(src_ids) + distract}


def synthetic_phase(plan: dict[str, Any], paths: dict[int, str], root: str,
                    beat: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Writes the transformed JPEGs and records the engine's dHash of each."""
    from scraper.image_phash import compute_dhash, hamming

    rng = random.Random(SEED + 1)
    out_dir = os.path.join(root, "synth")
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for n, iid in enumerate(plan["sources"], 1):
        if beat is not None and n % 100 == 0:
            beat(f"transforms {n}/{len(plan['sources'])}")
        with open(paths[iid], "rb") as fh:
            raw = fh.read()
        try:
            base = compute_dhash(raw)
            img = decode(paths[iid])
        except Exception:  # noqa: BLE001
            continue
        for t in SYNTH_TRANSFORMS:
            sid = f"{iid}:{t}"
            p = os.path.join(out_dir, f"{iid}_{t}.jpg")
            try:
                im = transform(img, t, rng)
                im.save(p, "JPEG", quality=95)
                with open(p, "rb") as fh:
                    dh = compute_dhash(fh.read())
            except Exception:  # noqa: BLE001
                continue
            rows.append({"synth_id": sid, "image_id": iid, "transform": t, "path": p,
                         "tag": plan["source_tag"].get(iid), "dhash_hamming": hamming(base, dh)})
    return rows


# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------

def past(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


def restorable(name: str) -> bool:
    """A result file (`x.npz`, `x.json`) or a shard of an unfinished arm
    (`x.shards/NNNNNN.npz`); nothing else, and never a path that climbs out."""
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts) or ".tmp." in name or name == "report.json":
        return False
    if len(parts) == 1:
        return name.endswith((".npz", ".json"))
    return len(parts) == 2 and parts[0].endswith(".shards") and parts[1].endswith(".npz")


def restore_checkpoint(out_dir: str, run_id: int) -> list[str]:
    """A re-dispatch of the same run on a NEW pod starts from the last uploaded tar, so a
    finished phase (its output file) is never paid for twice and an unfinished one resumes
    after its last uploaded shard. The tar streams to disk, never into RAM. Best effort."""
    from scraper import image_storage

    key = f"{RESULTS_PREFIX}/{run_id}/results.tar"
    r2 = image_storage.R2Client.from_env()
    if r2.object_size(key) is None:
        return []
    tar_path = out_dir.rstrip("/") + ".restore.tar"
    r2.download_file(key, tar_path)
    restored = []
    try:
        with tarfile.open(tar_path) as tar:
            for member in tar.getmembers():
                name = member.name.split("/", 1)[-1]
                if not member.isfile() or not restorable(name):
                    continue
                target = os.path.join(out_dir, name)
                finished = os.path.join(out_dir, name.split("/")[0][:-len(".shards")] + ".npz")
                if os.path.exists(target) or ("/" in name and os.path.exists(finished)):
                    continue
                src = tar.extractfile(member)
                if src is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target + ".part", "wb") as fh:
                    shutil.copyfileobj(src, fh)
                os.replace(target + ".part", target)
                restored.append(name)
    finally:
        os.remove(tar_path)
    return restored


def upload_results(out_dir: str, run_id: int, local_only: bool) -> str:
    tar_path = out_dir.rstrip("/") + ".tar"

    def settled(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if ".tmp." in info.name or info.name.endswith(".part") else info

    with tarfile.open(tar_path, "w") as tar:
        tar.add(out_dir, arcname="g1_results", filter=settled)
    if local_only:
        return tar_path
    from scraper import image_storage

    key = f"{RESULTS_PREFIX}/{run_id}/results.tar"
    image_storage.R2Client.from_env().upload_file(key, tar_path, content_type="application/x-tar")
    return key


def finalize(out_dir: str, run_id: int, rep: Reporter, reason: str,
             local_only: bool) -> int:
    """The bootstrap's last call when it gives the payload up (an OOM kill, or a third
    failure): ship what the passes left on disk — finished arms and the shards of the
    unfinished ones, which a `resume` continues — and close every open arm, so the
    dispatcher's watchdog ends the pod now instead of at the end of its window."""
    uploaded = "nothing on disk"
    if os.path.isdir(out_dir):
        path = os.path.join(out_dir, "report.json")
        try:
            with open(path) as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            report = {"run_id": run_id}
        report["gave_up"] = {"at": iso_now(), "reason": reason}
        try:
            with open(path, "w") as fh:
                json.dump(report, fh, indent=1, default=str)
            uploaded = upload_results(out_dir, run_id, local_only=local_only)
        except Exception as exc:  # noqa: BLE001 - closing the arms matters more
            LOG.exception("final upload failed")
            uploaded = f"upload failed: {type(exc).__name__}: {exc}"
    rep.run_note(alive=f"gave up ({reason}); uploaded {uploaded}")
    rep.fail_open_arms(f"payload gave up ({reason}); partial results: {uploaded}")
    rep.finish_run("failed")
    LOG.info("FINALIZE %s -> %s", reason, uploaded)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run-id", type=int, default=0)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--out", default="")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--max-seconds", type=float, default=0)
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--local-manifest", default="")
    ap.add_argument("--image-dir", default="")
    ap.add_argument("--smoke", action="store_true",
                    help="Tiny models and caps: proves the code path on a CPU.")
    ap.add_argument("--phases", default=",".join(PHASES))
    ap.add_argument("--lightglue-sha", default=LIGHTGLUE_SHA)
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--finalize", action="store_true",
                    help="Upload whatever is on disk, close every open arm and exit: the "
                         "bootstrap's give-up call (the reason travels in PODBOOT_GAVE_UP).")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    phases = [p for p in args.phases.split(",") if p]
    out_dir = args.out or os.path.join(args.root, f"run-{args.run_id}")
    os.makedirs(out_dir, exist_ok=True)
    deadline = time.monotonic() + args.max_seconds if args.max_seconds else None

    conn = None
    if not args.no_db:
        import psycopg

        conn = psycopg.connect(os.environ["SUPABASE_DB_URL"], autocommit=True,
                               prepare_threshold=None, connect_timeout=15)
    cached = [0]
    synth_dir = os.path.join(args.root, "synth")
    rep = Reporter(conn, args.run_id,
                   units_fn=lambda: durable_units(out_dir, synth_dir) + cached[0] // 1000)
    if args.finalize:
        return finalize(out_dir, args.run_id, rep,
                        os.environ.get("PODBOOT_GAVE_UP") or "the bootstrap gave up",
                        local_only=conn is None)
    rep.run_note(boot=f"g1 {sys.version.split()[0]}", alive="starting")

    if not args.skip_deps:
        rep.run_note(alive="deps")
        ensure_deps(args.lightglue_sha)
    versions = runtime_versions()
    rep.run_note(boot=versions, alive="deps ok")

    device = args.device
    if device != "cpu":
        import torch

        if not torch.cuda.is_available():
            LOG.warning("no GPU visible: falling back to cpu")
            device = "cpu"

    manifest_key = None
    if conn is not None and not args.local_manifest:
        with conn.cursor() as cur:
            cur.execute(_RUN_SQL, {"run_id": args.run_id})
            row = cur.fetchone()
        manifest_key = row[3] if row else None
    manifest = load_manifest(local=args.local_manifest or None, key=manifest_key)
    if args.smoke:
        manifest["pairs"] = manifest["pairs"][:20]
        keep = {i for p in manifest["pairs"] for i in (p["a"], p["b"])}
        nb = {k: v for k, v in (manifest.get("neighbours") or {}).items() if int(k) in keep}
        keep |= {int(s) for rows in nb.values() for s, _f, _i in rows}
        manifest["neighbours"] = nb
        manifest["images"] = [r for r in manifest["images"] if r["listing_id"] in keep]
        ids = {r["image_id"] for r in manifest["images"]}
        manifest["frame_pairs"] = [f for f in manifest.get("frame_pairs", [])
                                   if f[0] in ids and f[1] in ids]
    listing_of = {int(r["image_id"]): int(r["listing_id"]) for r in manifest["images"]}
    nb_images = {int(r["image_id"]) for r in manifest["images"] if r.get("nb")}
    report: dict[str, Any] = {"run_id": args.run_id, "versions": versions, "device": device,
                              "started": iso_now(), "phases": {}, "identity": {}}
    if conn is not None:
        try:
            report["restored"] = restore_checkpoint(out_dir, args.run_id)
            rep.run_note(alive=f"restored {len(report['restored'])} files from the checkpoint")
        except Exception as exc:  # noqa: BLE001 - a fresh start is the fallback
            LOG.warning("checkpoint restore failed: %s", exc)
    if device != "cpu":
        import torch

        report["gpu"] = torch.cuda.get_device_name(0)
    # Sized at run time from the pod RunPod actually gave us: the R2 fetch is I/O-bound and
    # keeps --workers; decode, RANSAC and torch's CPU threads get at most one per vCPU.
    vcpus = pod_vcpus()
    workers = cpu_workers(args.workers, vcpus)
    fetch_workers = args.workers or max(16, 2 * vcpus)
    # And from the RAM it gave us: the decode queue of every embed arm is sized from the
    # cgroup limit (plan_decode), and the image cache must be a disk, never tmpfs.
    mem = pod_memory()
    cache_dir = os.path.join(args.root, "img")
    report["cpu"] = {"vcpus": vcpus, "host_cpu_count": os.cpu_count(), "workers": workers,
                     "fetch_workers": fetch_workers}
    report["resources"] = {**mem, "vcpus": vcpus, "workers": workers,
                           "cache_dir": cache_dir, "cache_fs": fs_type(args.root)}
    if report["resources"]["cache_fs"] == "tmpfs":
        LOG.warning("the image cache %s is on tmpfs: every cached image costs RAM", cache_dir)

    def gb(n: int | None) -> str:
        return "?" if not n else f"{n / 2**30:.1f}GB"

    rep.run_note(alive=f"vcpus={vcpus} workers={workers} fetch_workers={fetch_workers} "
                       f"mem_limit={gb(mem['limit'])} mem_total={gb(mem['mem_total'])} "
                       f"cgroup={gb(mem['cgroup_limit'])} cache_fs={report['resources']['cache_fs']}")
    try:
        import torch

        torch.set_num_threads(vcpus)
    except ImportError:
        pass

    rep.run_note(alive=f"caching {len(manifest['images'])} images")
    t0 = time.monotonic()
    paths = cache_images(manifest["images"], cache_dir=cache_dir,
                         image_dir=args.image_dir or None, workers=fetch_workers,
                         beat=lambda m: rep.run_note(alive=m),
                         on_count=lambda n: cached.__setitem__(0, n))
    report["images"] = {"requested": len(manifest["images"]), "cached": len(paths),
                        "seconds": round(time.monotonic() - t0, 1)}
    json.dump(sorted(set(listing_of) - set(paths)), open(os.path.join(out_dir, "images_missing.json"), "w"))
    items = sorted(paths.items())

    def beat_for(phase):
        return lambda m: rep.beat(phase, m)

    last_upload = [time.monotonic()]

    def checkpoint(what: str, force: bool = False) -> None:
        """Local disk already holds the shard or arm; R2 gets the tar every CHECKPOINT_S."""
        if not force and time.monotonic() - last_upload[0] < CHECKPOINT_S:
            return
        last_upload[0] = time.monotonic()
        try:
            json.dump(report, open(os.path.join(out_dir, "report.json"), "w"), indent=1, default=str)
            upload_results(out_dir, args.run_id, local_only=conn is None)
            rep.run_note(alive=f"checkpoint uploaded: {what}")
        except Exception as exc:  # noqa: BLE001 - the next checkpoint or the final upload retries
            LOG.warning("checkpoint upload failed: %s", exc)

    def decode_plan(phase: str, requested_batch: int, image_bytes: int) -> tuple[int, int]:
        batch, prefetch = plan_decode(mem["limit"], workers, requested_batch, image_bytes)
        report.setdefault("decode_plan", {})[phase] = {
            "batch": batch, "prefetch": prefetch, "image_mb": round(image_bytes / 2**20, 2),
            "queue_gb": round((prefetch + 1) * batch * image_bytes / 2**30, 2)}
        return batch, prefetch

    status = "ok"
    try:
        dino_arm = dict(DINOV2_ARM)
        v3_arm = dict(DINOV3_ARM)
        clip_arm = dict(CLIP_ARM)
        heads_model, heads = (None, [])
        if conn is not None:
            heads_model, heads = read_heads(conn)
        if heads_model is not None:
            # The heads are only meaningful on their own encoder's population.
            for k in ("model", "revision", "library", "pooling", "resolution", "preprocessing", "dtype"):
                v3_arm[k] = heads_model[k]
        if args.smoke:
            dino_arm.update(model="facebook/dinov2-with-registers-small", resolution=224, dtype="fp32")
            v3_arm.update(model="facebook/dinov2-small", resolution=224, dtype="fp32")
            clip_arm.update(resolution=224)

        if "embed" in phases:
            rep.phase("embed", "running", "sscd")
            if not os.path.exists(os.path.join(out_dir, "emb_sscd.npz")):
                enc = SSCD(fetch_sscd(args.root), device)
                report["identity"]["sscd"] = {"model": "sscd_disc_mixup", "revision": enc.revision,
                                              "resolution": SSCD_SIZE,
                                              "preprocessing": "square_squash"}
                batch, prefetch = decode_plan("embed_sscd", 64, SSCD_SIZE**2 * PIL_RGB_BYTES)
                report["phases"]["embed_sscd"] = embed_arm(
                    "sscd", enc, items, batch=batch, workers=workers, loader=sscd_loader,
                    out_path=os.path.join(out_dir, "emb_sscd.npz"), beat=beat_for("embed"),
                    deadline=deadline, prefetch=prefetch, on_shard=checkpoint)
                del enc
                checkpoint("embed_sscd")
            # DINOv2 is the licence-clean comparison arm, read on the labelled pairs only;
            # DINOv3 also embeds the neighbourhood, because its heads route those frames.
            pair_items = [it for it in items if it[0] not in nb_images]
            for name, arm, arm_items in (("dinov2", dino_arm, pair_items), ("dinov3", v3_arm, items)):
                out_path = os.path.join(out_dir, f"emb_{name}.npz")
                if os.path.exists(out_path):
                    report["phases"][f"embed_{name}"] = {"arm": name, "skipped": "exists"}
                    continue
                rep.phase("embed", "running", name)
                enc, rev = load_hf_encoder(arm, arm.get("revision") or None, device, threads=vcpus)
                report["identity"][name] = {**arm, "revision": rev}
                batch, prefetch = decode_plan(f"embed_{name}", args.batch_size,
                                              int(arm["resolution"])**2 * PIL_RGB_BYTES)
                report["phases"][f"embed_{name}"] = embed_arm(
                    name, enc, arm_items, batch=batch, workers=workers,
                    loader=preprocessed_loader(arm["preprocessing"], int(arm["resolution"])),
                    out_path=out_path, beat=beat_for("embed"), deadline=deadline,
                    prefetch=prefetch, on_shard=checkpoint)
                del enc
                checkpoint(f"embed_{name}")
            embeds = {k: v for k, v in report["phases"].items() if k.startswith("embed")}
            # An arm cut by the deadline is unfinished (its shards wait for a resume): the
            # row says so rather than "ok".
            rep.phase("embed", "failed" if any(v.get("partial") for v in embeds.values())
                      else "ok", json.dumps(embeds)[:1800])
            # A checkpoint upload: the descriptors alone answer half the question, and a
            # watchdog teardown later must not cost them.
            checkpoint("after embed", force=True)

        import numpy as np

        dino = sscd = None
        if os.path.exists(os.path.join(out_dir, "emb_dinov3.npz")):
            dino = load_vecs(os.path.join(out_dir, "emb_dinov3.npz"))
        if os.path.exists(os.path.join(out_dir, "emb_sscd.npz")):
            sscd = load_vecs(os.path.join(out_dir, "emb_sscd.npz"))

        heads_pack = None
        heads_path = os.path.join(out_dir, "heads_v1.npz")
        if os.path.exists(heads_path):
            z = np.load(heads_path)
            heads_pack = {k: z[k] for k in z.files}
            rep.phase("heads", "ok", "restored")
        elif "heads" in phases:
            if heads and dino is not None and not args.smoke:
                heads_pack = score_heads(dino[0], dino[1], heads)
                np.savez(os.path.join(out_dir, "heads_v1.npz"), **heads_pack)
                json.dump({"model": heads_model, "rooms": {str(k): v for k, v in head_room_map(heads).items()},
                           "heads": [{"tag_id": h["tag_id"], "label": h["label"],
                                      "threshold": h["artifact"].get("threshold")} for h in heads]},
                          open(os.path.join(out_dir, "heads_v1.json"), "w"), default=str, indent=1)
                rep.phase("heads", "ok", f"{len(heads)} heads over {len(dino[0])} images")
            else:
                rep.phase("heads", "skipped", "no active model, no DINOv3 vectors, or smoke")

        room_map = head_room_map(heads)
        report["head_rooms"] = {str(k): v for k, v in sorted(room_map.items())}
        routes = build_routes(manifest, dino=dino, sscd=sscd, heads=heads_pack, room_map=room_map)
        pairs = lg_plan(routes, manifest, rooms_of(manifest, heads_pack, room_map))
        every = sorted(routes)
        np.savez(os.path.join(out_dir, "frame_pairs.npz"),
                 a=np.array([p[0] for p in every], dtype=np.int64),
                 b=np.array([p[1] for p in every], dtype=np.int64),
                 route=np.array([routes[p] for p in every], dtype=np.int64),
                 lg_a=np.array([p[0] for p in pairs], dtype=np.int64),
                 lg_b=np.array([p[1] for p in pairs], dtype=np.int64))
        report["routes"] = {"routed": len(every), "lightglue": len(pairs)}
        rep.phase("route", "ok", f"{len(every)} routed, {len(pairs)} for LightGlue")

        synth_done = all(os.path.exists(os.path.join(out_dir, f)) for f in (
            "synthetic.json", "syn_sscd.npz", "syn_dinov2.npz", "syn_dinov3.npz", "syn_clip.npz",
            "gal_clip.npz"))
        if "synthetic" in phases and synth_done:
            rep.phase("synthetic", "ok", "restored")
        elif "synthetic" in phases and past(deadline):
            rep.phase("synthetic", "failed", "deadline reached before the phase")
        elif "synthetic" in phases:
            try:
                rep.phase("synthetic", "running", "transforms")
                plan = synthetic_plan(manifest, paths, 60 if args.smoke else SYNTH_N,
                                      200 if args.smoke else SYNTH_GALLERY)
                rows = synthetic_phase(plan, paths, args.root, beat=beat_for("synthetic"))
                json.dump({"plan": {k: v for k, v in plan.items() if k != "source_tag"},
                           "rows": [{k: v for k, v in r.items() if k != "path"} for r in rows]},
                          open(os.path.join(out_dir, "synthetic.json"), "w"))
                q_items = [(r["synth_id"], r["path"]) for r in rows]
                g_items = [(i, paths[i]) for i in plan["gallery"] if i in paths]
                enc = SSCD(fetch_sscd(args.root), device)
                batch, prefetch = decode_plan("synth_sscd", 64, SSCD_SIZE**2 * PIL_RGB_BYTES)
                report["phases"]["synth_sscd"] = embed_arm(
                    "synth_sscd", enc, q_items, batch=batch, workers=workers, loader=sscd_loader,
                    out_path=os.path.join(out_dir, "syn_sscd.npz"), beat=beat_for("synthetic"),
                    deadline=None, prefetch=prefetch, on_shard=checkpoint)
                del enc
                for name, arm in (("dinov2", dino_arm), ("dinov3", v3_arm), ("clip", clip_arm)):
                    if name == "clip":
                        # The incumbent through PRODUCTION's own embedder (the pinned
                        # CLIPModel + CLIPProcessor that wrote image_clip_embeddings), not a
                        # second loader: transformers 4.57 cannot build
                        # CLIPVisionModelWithProjection from this checkpoint's CLIPConfig.
                        enc, rev = load_production_clip(threads=vcpus)
                        loader = decode
                        image_bytes = FULL_DECODE_BYTES
                    else:
                        enc, rev = load_hf_encoder(arm, report["identity"].get(name, {}).get("revision")
                                                   or arm.get("revision") or None, device,
                                                   threads=vcpus)
                        loader = preprocessed_loader(arm["preprocessing"], int(arm["resolution"]))
                        image_bytes = int(arm["resolution"])**2 * PIL_RGB_BYTES
                    report["identity"].setdefault(name, {**arm, "revision": rev})
                    batch, prefetch = decode_plan(f"synth_{name}", args.batch_size, image_bytes)
                    report["phases"][f"synth_{name}"] = embed_arm(
                        f"synth_{name}", enc, q_items, batch=batch, workers=workers,
                        loader=loader, out_path=os.path.join(out_dir, f"syn_{name}.npz"),
                        beat=beat_for("synthetic"), deadline=None, prefetch=prefetch,
                        on_shard=checkpoint)
                    if name == "clip":
                        report["phases"]["gallery_clip"] = embed_arm(
                            "gallery_clip", enc, g_items, batch=batch, workers=workers,
                            loader=loader, out_path=os.path.join(out_dir, "gal_clip.npz"),
                            beat=beat_for("synthetic"), deadline=None, prefetch=prefetch,
                            on_shard=checkpoint)
                    del enc
                rep.phase("synthetic", "ok", f"{len(rows)} transformed copies")
            except Exception as exc:  # noqa: BLE001 - the matchers still run
                LOG.exception("synthetic failed")
                rep.phase("synthetic", "failed", f"{type(exc).__name__}: {exc}")
                status = "failed"

        for ex in EXTRACTORS:
            phase = f"match-{ex}"
            if phase not in phases:
                continue
            if past(deadline):
                rep.phase(phase, "failed", "deadline reached before the phase")
                continue
            rep.phase(phase, "running", "starting")
            try:
                report["phases"][phase] = match_phase(
                    ex, pairs, paths, device=device, out_path=os.path.join(out_dir, f"lg_{ex}.npz"),
                    beat=beat_for(phase), deadline=deadline, workers=min(8, workers),
                    on_shard=checkpoint)
                rep.phase(phase, "failed" if report["phases"][phase].get("partial") else "ok",
                          json.dumps(report["phases"][phase]))
            except Exception as exc:  # noqa: BLE001 - one extractor must not lose the other
                LOG.exception("%s failed", phase)
                rep.phase(phase, "failed", f"{type(exc).__name__}: {exc}")
                status = "failed"

    except Exception as exc:  # noqa: BLE001 - upload whatever exists
        LOG.exception("payload failed")
        status = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["finished"] = iso_now()
        json.dump(report, open(os.path.join(out_dir, "report.json"), "w"), indent=1, default=str)
        try:
            where = upload_results(out_dir, args.run_id, local_only=conn is None)
            rep.phase("upload", "ok", where)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("upload failed")
            rep.phase("upload", "failed", f"{type(exc).__name__}: {exc}")
            status = "failed"
        # Every g1 arm row must be terminal so the watchdog ends the pod at once.
        for phase in PHASES + tuple(p for p in OPTIONAL_PHASES if p in phases):
            if phase not in rep.terminal:
                rep.phase(phase, "skipped" if phase not in phases else "failed",
                          "not requested" if phase not in phases else "not reached")
        rep.finish_run(status)
        rep.run_note(alive=f"done status={status}")
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
