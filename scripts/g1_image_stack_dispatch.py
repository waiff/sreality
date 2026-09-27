"""G1 image-stack bake-off: the dispatcher (GitHub Actions runner).

Three stages, one workflow run (`tagging_bakeoff.yml`, stage `g1-image-stack`):

  prepare  Exports the frozen pair list's adverts BY ID with the engine's own exporter
           (`autodedup.export.run_export`, the same SQL the lane reads: images, dHash,
           corpus pop, stored CLIP B/32, CLIP tags, price paths), assembles the manifest
           with `scripts.g1_image_pairs.assemble`, creates the progress rows the tagging
           watchdog already reads (`dedup_sim.tag_head_bakeoff_runs/arms`, label
           `g1-image-stack`, one arm row per pod phase) and uploads the manifest to R2.
  pod      Rents one GPU through the tagging lane's launcher, bootstrap and watchdog
           (`scripts.tagging_bakeoff_dispatch`: fetch by sha, uv 3.12, cu118 torch +
           torchvision, step heartbeats, crash-loop and stall teardown) and runs
           `scripts.g1_image_stack_pod`. THE ONLY STAGE THAT SPENDS MONEY.
  collect  Downloads `bakeoff/g1-image-stack/<run>/results.tar` from R2 into --out, so
           the workflow uploads it as an artifact the coordinator reads offline.

`--stage all` runs the three in order; `--stage resume` runs pod then collect for an
existing run: the new pod restores the last checkpoint tar from R2 and pays only for the
phases (or the LightGlue tail) the previous pod did not finish. Never re-run `prepare` on a
run that has results: it would re-export a live, moved corpus under the same run id. `--dry-run` exports and assembles (free, and the
manifest counts are the point of a dry run) but writes no row, uploads nothing and rents
nothing; the pod stage then only runs the bootstrap's offline preflight.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import sys
import tarfile
from argparse import Namespace
from typing import Any, Sequence

from scripts import g1_image_pairs as pairs_mod
from scripts import pod_bootstrap
from scripts import tagging_bakeoff_dispatch as tb

LOG = logging.getLogger("g1_image_stack_dispatch")

PAIRS_PATH = "data/g1_image_stack/pairs.json.gz"
LABEL = "g1-image-stack"
PREFIX = "bakeoff/g1-image-stack"
MODULE = "scripts.g1_image_stack_pod"
PHASES = ("embed", "heads", "route", "synthetic", "match-aliked", "match-disk", "upload")
OPTIONAL_PHASES = ("match-superpoint",)
STARTUP_GRACE_S = 1200
SIDE_FILES = (("manifest.json.gz", "application/gzip"),
              ("clip_b32_stored.npz", "application/octet-stream"),
              ("counts.json", "application/json"))
DEFAULT_MAX_SECONDS = 4 * 3600

_CREATE_RUN_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_runs (label, note, status)
    VALUES (%(label)s, %(note)s, 'running') RETURNING id
"""
_CREATE_ARM_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_arms
        (run_id, arm, model, revision, library, pooling, resolution, preprocessing, dtype, status, note)
    VALUES (%(run_id)s, %(arm)s, %(model)s, '', 'g1', 'n/a', 1, 'n/a', 'n/a', 'pending', %(note)s)
    ON CONFLICT (run_id, arm) DO UPDATE SET status = 'pending', note = EXCLUDED.note
"""
_SET_MANIFEST_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_runs SET manifest_key = %(key)s WHERE id = %(run_id)s
"""
_RESET_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms SET status = 'pending'
    WHERE run_id = %(run_id)s AND arm LIKE 'g1:%%' AND status IN ('failed', 'skipped', 'ok')
"""


def export_by_ids(listing_ids: Sequence[int], out_dir: str) -> dict[str, Any]:
    """The engine's exporter over an explicit id list: one pseudo-block whose id query is
    replaced by the list. Every record is built by the same code the lane reads."""
    from autodedup import export
    from scraper import db

    ids = sorted({int(i) for i in listing_ids})
    original = export.fetch_block_ids
    export.fetch_block_ids = lambda conn, block, **kw: (ids, {"by_id": len(ids)})
    try:
        return export.run_export(db.connect, {"blocks": "town:0"}, out_dir)
    finally:
        export.fetch_block_ids = original


def load_single_export(path: str) -> tuple[dict[int, Any], dict[int, list[Any]]]:
    adverts: dict[int, Any] = {}
    with gzip.open(path, "rt") as fh:
        for line in fh:
            if line.startswith('{"t": "listing"'):
                a = pairs_mod._advert("by_id", json.loads(line))
                adverts[a.id] = a
    _, imgs = pairs_mod.scan_images(("by_id", path, frozenset(adverts), True))
    images: dict[int, list[Any]] = {}
    for img in imgs:
        images.setdefault(img.listing_id, []).append(img)
    for lst in images.values():
        lst.sort(key=lambda i: (i.seq, i.image_id))
    return adverts, images


def prepare(args: Namespace) -> int:
    with gzip.open(args.pairs, "rt") as fh:
        doc = json.load(fh)
    os.makedirs(args.out, exist_ok=True)
    LOG.info("pairs %d over %d listings", len(doc["pairs"]), len(doc["listing_ids"]))
    summary = export_by_ids(doc["listing_ids"], args.out)
    LOG.info("export %s", json.dumps({k: summary[k] for k in ("counts", "timings", "errors")},
                                     default=str))
    adverts, images = load_single_export(os.path.join(args.out, "cohort.jsonl.gz"))
    manifest, counts = pairs_mod.assemble(doc["pairs"], adverts, images, doc.get("neighbours"))
    need = {r["image_id"] for r in manifest["images"]}
    counts["clip_vectors"] = pairs_mod.write_clip(os.path.join(args.out, "clip_b32_stored.npz"),
                                                  images, need)
    pairs_mod.write_manifest(os.path.join(args.out, "manifest.json.gz"), manifest)
    json.dump(counts, open(os.path.join(args.out, "counts.json"), "w"), indent=1)
    LOG.info("manifest %s", json.dumps(counts))
    if args.dry_run:
        LOG.info("DRY RUN: no run row, no R2 upload.")
        return 0
    with tb._connect(os.environ["SUPABASE_DB_URL"]) as conn, conn.cursor() as cur:
        if not args.run_id:
            cur.execute(_CREATE_RUN_SQL, {"label": LABEL, "note": f"g1 manifest {json.dumps(counts)[:1500]}"})
            args.run_id = int(cur.fetchone()[0])
        for phase in PHASES:
            cur.execute(_CREATE_ARM_SQL, {"run_id": args.run_id, "arm": f"g1:{phase}",
                                          "model": LABEL, "note": "created by prepare"})
        key = f"{PREFIX}/{args.run_id}/manifest.json.gz"
        from scraper import image_storage

        r2 = image_storage.R2Client.from_env()
        # The evaluator's inputs travel with the run, so a `resume` (no prepare) still
        # collects everything the offline read needs.
        for name, ctype in SIDE_FILES:
            with open(os.path.join(args.out, name), "rb") as fh:
                r2.upload_bytes(f"{PREFIX}/{args.run_id}/{name}", fh.read(), content_type=ctype)
        cur.execute(_SET_MANIFEST_SQL, {"key": key, "run_id": args.run_id})
    with open(os.path.join(args.out, "run_id"), "w") as fh:
        fh.write(str(args.run_id))
    LOG.info("RUN_ID=%d manifest=%s", args.run_id, key)
    return 0


def pod(args: Namespace) -> int:
    payload = [f"--run-id={args.run_id}", "--device=cuda", f"--workers={args.workers}",
               f"--batch-size={args.batch_size}", f"--max-seconds={int(args.job_max_seconds)}"]
    if args.phases:
        payload.append(f"--phases={args.phases}")
    start_cmd = pod_bootstrap.build_start_cmd(ref=args.ref, module=MODULE, payload_args=payload,
                                              extra="clip")
    plan = tb.Plan(stage="embed", where="pod", payload_args=payload, start_cmd=start_cmd,
                   max_wait_s=args.job_max_seconds + STARTUP_GRACE_S, execute=not args.dry_run)
    if not args.dry_run:
        with tb._connect(os.environ["SUPABASE_DB_URL"]) as conn, conn.cursor() as cur:
            cur.execute(_RESET_SQL, {"run_id": args.run_id})
    shim = Namespace(run_id=args.run_id, arms=",".join(f"g1:{p}" for p in PHASES),
                     force_arms=False, image=args.image, ref=args.ref,
                     container_disk_gb=args.container_disk_gb, gpu_allowlist=args.gpu_allowlist,
                     job_max_seconds=args.job_max_seconds,
                     bootstrap_deadline_s=args.bootstrap_deadline_s,
                     stall_deadline_s=args.stall_deadline_s)
    tb._log_arm_reset = lambda *a, **k: None  # the g1 reset above is the new attempt
    return tb._run_pod(plan, shim)


def collect(args: Namespace) -> int:
    from scraper import image_storage

    key = f"{PREFIX}/{args.run_id}/results.tar"
    r2 = image_storage.R2Client.from_env()
    if r2.object_size(key) is None:
        LOG.error("no results at %s", key)
        return 1
    os.makedirs(args.out, exist_ok=True)
    data = r2.download_bytes(key)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(args.out, filter="data")
    LOG.info("collected %s (%d bytes) into %s", key, len(data), args.out)
    for name, _ctype in SIDE_FILES:
        target = os.path.join(args.out, name)
        side = f"{PREFIX}/{args.run_id}/{name}"
        if not os.path.exists(target) and r2.object_size(side) is not None:
            with open(target, "wb") as fh:
                fh.write(r2.download_bytes(side))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--stage", choices=("prepare", "pod", "collect", "all", "resume"),
                    required=True)
    ap.add_argument("--phases", default="",
                    help="Pod phases, comma-separated (default: all of PHASES). "
                         "match-superpoint is licence-gated: an explicit operator decision.")
    ap.add_argument("--run-id", type=int, default=0)
    ap.add_argument("--pairs", default=PAIRS_PATH)
    ap.add_argument("--out", default="g1_out")
    ap.add_argument("--ref", default=os.environ.get("GITHUB_SHA") or "main")
    ap.add_argument("--image", default=tb.DEFAULT_IMAGE)
    ap.add_argument("--container-disk-gb", type=int, default=100)
    ap.add_argument("--gpu-allowlist", default=",".join(tb.DEFAULT_GPU_ALLOWLIST))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--job-max-seconds", type=float, default=DEFAULT_MAX_SECONDS)
    ap.add_argument("--bootstrap-deadline-s", type=float, default=tb.DEFAULT_BOOTSTRAP_DEADLINE_S)
    ap.add_argument("--stall-deadline-s", type=float, default=tb.DEFAULT_STALL_DEADLINE_S)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if args.container_disk_gb < tb.MIN_CONTAINER_DISK_GB:
        LOG.error("container disk below %d GB", tb.MIN_CONTAINER_DISK_GB)
        return 2
    stages = {"all": ("prepare", "pod", "collect"),
              "resume": ("pod", "collect")}.get(args.stage, (args.stage,))
    if args.phases and not set(args.phases.split(",")) <= set(PHASES + OPTIONAL_PHASES):
        LOG.error("unknown phase in %s; known: %s", args.phases, ",".join(PHASES + OPTIONAL_PHASES))
        return 2
    for stage in stages:
        if stage != "prepare" and not args.run_id and not args.dry_run:
            LOG.error("stage %s needs --run-id (prepare mints one)", stage)
            return 2
        if stage == "collect" and args.dry_run:
            LOG.info("DRY RUN: collect skipped")
            continue
        rc = {"prepare": prepare, "pod": pod, "collect": collect}[stage](args)
        LOG.info("stage %s rc=%d", stage, rc)
        if rc != 0 and stage != "pod":
            return rc
        if rc != 0 and stage == "pod":
            # A failed pod still may have uploaded partial results: collect regardless.
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
