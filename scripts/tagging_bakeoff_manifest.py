"""Bootstrap one tagging bake-off run: the run row, the arm rows, the image manifest,
and the free `clip-b32-stored` baseline.

Stage 1 of 3 (`.github/workflows/tagging_bakeoff.yml`). Runs on a plain GitHub runner
with SUPABASE_DB_URL and the R2_* secrets; installs no torch, downloads no weights,
launches no pod, spends nothing.

WHAT IT PRODUCES:
  * one `dedup_sim.tag_head_bakeoff_runs` row (status 'running'),
  * one `dedup_sim.tag_head_bakeoff_arms` row per arm (status 'pending'),
  * `bakeoff/tagging/<run_id>/manifest.json` in R2 — one 7-day presigned GET URL per
    image, so the GPU pod that consumes it needs NO database and NO R2 credentials to
    read the pictures,
  * the `clip-b32-stored` arm's vectors, COPIED by SQL out of the live
    `image_clip_embeddings`. That arm is the incumbent baseline every other arm is
    measured against and it costs zero GPU seconds, so it is done here rather than
    dispatched to a pod that would have nothing to compute.

THE IMAGE SET IS ASSEMBLED FROM SANCTIONED READERS ONLY. Every training image arrives
through `toolkit.machine_labeling.training_rows`, the one door onto training labels
(holdout-excluded, `in_training`-only); the exam images arrive through a
`tag_exam_members` read, which is a membership question, not a label question. This
module contains NO SQL naming `image_tag_labels`, deliberately — see
`tests/test_holdout_exclusion_census.py` for why the clean path is to never write one.

Note what that means for the exam: its images ARE embedded (they have to be — the run's
whole point is to grade the heads on them) but no exam ANSWER is read here. Embedding a
picture leaks nothing; reading its label would.

HEAD SELECTION IS THE OPERATOR'S READY FLAG (ruling 2026-09-08), read through the ONE
shared selector `toolkit.tag_head_bakeoff.ready_heads` — the same door the CPU runner
uses, so the two halves of the lane cannot pick different heads. `--heads` overrides it
with an explicit list. Admitted positive/negative counts are reported on every selected
head, in the log and on the run row, but they never filter: a ready head with too few
rows enters the run and its failure is recorded per cell.

Usage:  python -m scripts.tagging_bakeoff_manifest --label "res+precision grid" --dry-run
Required: SUPABASE_DB_URL (+ R2_* to mint URLs and upload the manifest).
Requires the sibling PR's dedup_sim.tag_head_bakeoff_* tables to have been applied.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Sequence

from scripts import tagging_bakeoff_arms as arms_mod
from toolkit import machine_labeling
from toolkit import tag_head_bakeoff as bo

LOG = logging.getLogger("tagging_bakeoff_manifest")

MANIFEST_PREFIX = "bakeoff/tagging"
DEFAULT_EXPIRES_S = 7 * 24 * 3600
# One bounded array parameter per statement, however many images the run holds.
_ID_CHUNK = 2000

# The sealed exam's membership, purpose-narrowed the same way tag_holdout's exclusion
# is: 'holdout' cohorts are the ones that GRADE, so they are the ones whose images must
# carry a vector under every arm. 'curated' cohort members are training material by the
# operator's ruling and therefore already arrive through training_rows.
_HOLDOUT_MEMBERS_SQL = """
    SELECT DISTINCT m.image_id
    FROM tag_exam_members m
    JOIN tag_exam_cohorts c ON c.id = m.cohort_id AND c.purpose = 'holdout'
"""

# storage_path IS NOT NULL is the "we hold the bytes" test: an image row without one has
# a URL we never fetched, so no presigned URL could serve it.
_STORED_IMAGES_SQL = """
    SELECT i.id, i.storage_path
    FROM images i
    WHERE i.id = ANY(%(ids)s::bigint[])
      AND i.storage_path IS NOT NULL
    ORDER BY i.id
"""

_INSERT_RUN_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_runs
      (label, note, status, heads, min_train_positives)
    VALUES (%(label)s, %(note)s, 'running', %(heads)s::bigint[], %(min_train_positives)s)
    RETURNING id
"""

_SET_MANIFEST_KEY_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_runs
    SET manifest_key = %(manifest_key)s
    WHERE id = %(run_id)s
"""

_FAIL_RUN_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_runs
    SET status = 'failed', note = %(note)s
    WHERE id = %(run_id)s
"""

_INSERT_ARM_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_arms
      (run_id, arm, model, revision, library, pooling, resolution, preprocessing,
       dtype, status, note)
    VALUES (%(run_id)s, %(arm)s, %(model)s, %(revision)s, %(library)s, %(pooling)s,
            %(resolution)s, %(preprocessing)s, %(dtype)s, 'pending', %(note)s)
    ON CONFLICT (run_id, arm) DO NOTHING
    RETURNING id
"""

_ARM_ID_SQL = """
    SELECT id FROM dedup_sim.tag_head_bakeoff_arms
    WHERE run_id = %(run_id)s AND arm = %(arm)s
"""

# The zero-GPU baseline. A cast, not a recomputation: the incumbent's vector(512) rows
# are the very numbers the live system uses, so copying them is the only way the
# comparison is against what production actually does rather than a re-run of it.
_COPY_STORED_CLIP_SQL = """
    INSERT INTO dedup_sim.tag_head_bakeoff_vectors (arm_id, image_id, embedding)
    SELECT %(arm_id)s, e.image_id, e.embedding::halfvec
    FROM image_clip_embeddings e
    WHERE e.model = %(model)s::text
      AND e.image_id = ANY(%(ids)s::bigint[])
    ON CONFLICT DO NOTHING
"""

_FINISH_ARM_SQL = """
    UPDATE dedup_sim.tag_head_bakeoff_arms
    SET status = %(status)s, dim = %(dim)s, note = %(note)s
    WHERE id = %(arm_id)s
"""


def select_heads(conn: Any, *,
                 head_ids: Sequence[int] | None = None) -> list[dict[str, Any]]:
    """The heads the operator marked ready (or the explicit list), plus their sizes.

    Selection is `tag_head_bakeoff.ready_heads` — the shared selector, so this stage
    and the CPU runner cannot disagree about scope. The counts come from
    `training_rows`, which IS the training population (in_training only, sealed exam
    excluded), so a head planned here and a head trained later cannot disagree about
    its own size either. They are reported, never applied as a threshold.
    """
    heads: list[dict[str, Any]] = []
    for tag_id, label in bo.ready_heads(conn, tag_ids=head_ids):
        rows = machine_labeling.training_rows(conn, tag_id=tag_id)
        positives = sum(1 for _, state in rows if state == "positive")
        negatives = len(rows) - positives
        heads.append({
            "tag_id": tag_id, "label": label,
            "positives": positives, "negatives": negatives,
            "image_ids": sorted({image_id for image_id, _ in rows}),
        })
    return heads


def head_census(heads: Sequence[dict[str, Any]]) -> str:
    """The selected heads and their set sizes, as one line for the run's note."""
    return "; ".join(f"{h['label']}({h['tag_id']}) {h['positives']}+/{h['negatives']}-"
                     for h in heads)


def holdout_image_ids(conn: Any) -> list[int]:
    """Every image under sealed-exam protection — the cohort the heads are graded on."""
    with conn.cursor() as cur:
        cur.execute(_HOLDOUT_MEMBERS_SQL)
        return sorted(int(r[0]) for r in cur.fetchall())


def stored_images(conn: Any, image_ids: Sequence[int]) -> dict[int, str]:
    """{image_id: storage_path} for the ids whose bytes we actually hold."""
    out: dict[int, str] = {}
    ids = list(image_ids)
    for start in range(0, len(ids), _ID_CHUNK):
        chunk = [int(i) for i in ids[start:start + _ID_CHUNK]]
        with conn.cursor() as cur:
            cur.execute(_STORED_IMAGES_SQL, {"ids": chunk})
            for image_id, key in cur.fetchall():
                out[int(image_id)] = str(key)
    return out


def build_arms(*, only: Sequence[str] | None, hf_token: str | None,
               siglip_probe: Any = None) -> list[arms_mod.Arm]:
    """The preset, narrowed by `--arms`, with SigLIP2's checkpoint resolved live."""
    if siglip_probe is None:
        siglip_probe = arms_mod.resolve_siglip_checkpoint
    siglip_model, siglip_resolution = siglip_probe(token=hf_token)
    preset = arms_mod.default_arms(siglip_model=siglip_model,
                                   siglip_resolution=siglip_resolution)
    preset = [arms_mod.renamed_to_effective(a) for a in preset]
    return arms_mod.select_arms(preset, only)


def _create_run(conn: Any, *, label: str, note: str, heads: Sequence[int]) -> int:
    with conn.cursor() as cur:
        cur.execute(_INSERT_RUN_SQL, {
            "label": label, "note": note,
            "heads": [int(h) for h in heads],
            # RETIRED 2026-09-08: selection is the operator's ready flag, not a
            # count. The column stays (the read API and the page echo it) and is
            # written 0 so no run can be read as having had a floor.
            "min_train_positives": 0,
        })
        row = cur.fetchone()
    if not row:
        raise RuntimeError("run insert returned no id")
    return int(row[0])


def _create_arms(conn: Any, *, run_id: int,
                 arms: Sequence[arms_mod.Arm]) -> dict[str, int]:
    """{arm name: arm_id}. Idempotent — a re-run against the same run_id reuses rows."""
    ids: dict[str, int] = {}
    for arm in arms:
        params = {**arm.identity(revision=None), "run_id": run_id, "note": arm.note}
        with conn.cursor() as cur:
            cur.execute(_INSERT_ARM_SQL, params)
            row = cur.fetchone()
            if row is None:
                cur.execute(_ARM_ID_SQL, {"run_id": run_id, "arm": arm.name})
                row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"could not resolve arm id for {arm.name}")
        ids[arm.name] = int(row[0])
    return ids


def copy_stored_clip(conn: Any, *, arm_id: int, image_ids: Sequence[int]) -> int:
    """Copy the incumbent's live vectors for this run's images. Returns rows present."""
    ids = [int(i) for i in image_ids]
    for start in range(0, len(ids), _ID_CHUNK):
        with conn.cursor() as cur:
            cur.execute(_COPY_STORED_CLIP_SQL, {
                "arm_id": arm_id, "model": arms_mod.STORED_CLIP_MODEL,
                "ids": ids[start:start + _ID_CHUNK],
            })
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*)::bigint FROM dedup_sim.tag_head_bakeoff_vectors "
            "WHERE arm_id = %(arm_id)s", {"arm_id": arm_id})
        row = cur.fetchone()
    return int(row[0]) if row else 0


def manifest_document(*, run_id: int, label: str, heads: Sequence[dict[str, Any]],
                      arms: Sequence[arms_mod.Arm], urls: dict[int, dict[str, str]],
                      exam_ids: Sequence[int], expires_in: int) -> dict[str, Any]:
    """The pod's whole world: which pictures, where to fetch them, and what for."""
    return {
        "kind": "tagging_bakeoff_manifest",
        "version": 1,
        "run_id": run_id,
        "label": label,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_in": expires_in,
        "heads": [
            {k: v for k, v in head.items() if k != "image_ids"} for head in heads
        ],
        "arms": [
            {**arm.identity(revision=None), "requested_resolution": arm.resolution}
            for arm in arms
        ],
        "exam_image_ids": [int(i) for i in exam_ids],
        "images": {str(image_id): meta for image_id, meta in sorted(urls.items())},
    }


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", default="", help="Short operator-facing name for the run.")
    p.add_argument("--note", default="", help="Free text recorded on the run row.")
    p.add_argument("--heads", default="",
                   help="Comma-separated tag ids. Overrides the operator's ready "
                        "flag entirely — an explicit list is the operator's call.")
    p.add_argument("--arms", default="",
                   help="Comma-separated arm names to narrow the preset to. Empty = "
                        "every arm. An unknown name is an error, not a silent no-op.")
    p.add_argument("--expires", type=int, default=DEFAULT_EXPIRES_S,
                   help="Presigned URL lifetime in seconds (default 7 days, R2's max).")
    p.add_argument("--dry-run", action="store_true",
                   help="Report the heads, the image counts and the arm list, then "
                        "exit. Creates no run row, mints no URL, copies no vector.")
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

    head_ids = [int(h) for h in args.heads.replace(",", " ").split() if h.strip()]
    arm_names = [a.strip() for a in args.arms.split(",") if a.strip()]
    hf_token = os.environ.get("HF_TOKEN")

    arms = build_arms(only=arm_names, hf_token=hf_token)
    LOG.info("BAKEOFF arms=%d: %s", len(arms), ", ".join(a.name for a in arms))

    import psycopg

    from scraper import image_storage

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        heads = select_heads(conn, head_ids=head_ids or None)
        LOG.info("BAKEOFF head selection = %s",
                 f"explicit --heads {head_ids}" if head_ids
                 else "tag_taxonomy ready flag (the training-set page toggle)")
        if not heads:
            LOG.error("no head is marked ready for training (and --heads is empty) "
                      "— flip the Ready toggle on /new-dedup/training-set first")
            return 1
        training_ids = {i for head in heads for i in head["image_ids"]}
        exam_ids = holdout_image_ids(conn)
        wanted = sorted(training_ids | set(exam_ids))
        stored = stored_images(conn, wanted)
        missing = len(wanted) - len(stored)

        LOG.info("BAKEOFF heads=%d training_images=%d exam_images=%d union=%d "
                 "stored=%d missing_bytes=%d",
                 len(heads), len(training_ids), len(exam_ids), len(wanted),
                 len(stored), missing)
        for head in heads:
            LOG.info("  head %d %-24s positives=%-5d negatives=%-5d",
                     head["tag_id"], head["label"], head["positives"],
                     head["negatives"])

        if args.dry_run:
            LOG.info("BAKEOFF dry_run — no run row, no manifest, no vectors copied.")
            return 0

        if not image_storage.is_configured():
            LOG.error("R2 env vars missing — the manifest's whole point is presigned "
                      "URLs, so this cannot proceed without them")
            return 1

        note = "; ".join(p for p in (args.note, f"heads: {head_census(heads)}") if p)
        run_id = _create_run(conn, label=args.label, note=note,
                             heads=[h["tag_id"] for h in heads])
        LOG.info("BAKEOFF run_id=%d created", run_id)
        try:
            arm_ids = _create_arms(conn, run_id=run_id, arms=arms)
            r2 = image_storage.R2Client.from_env()
            urls = {
                image_id: {"key": key,
                           "url": r2.presigned_get(key, expires_in=args.expires)}
                for image_id, key in stored.items()
            }
            document = manifest_document(
                run_id=run_id, label=args.label, heads=heads, arms=arms, urls=urls,
                exam_ids=exam_ids, expires_in=args.expires)
            manifest_key = f"{MANIFEST_PREFIX}/{run_id}/manifest.json"
            r2.upload_bytes(manifest_key,
                            json.dumps(document).encode("utf-8"),
                            content_type="application/json")
            with conn.cursor() as cur:
                cur.execute(_SET_MANIFEST_KEY_SQL,
                            {"manifest_key": manifest_key, "run_id": run_id})
            LOG.info("BAKEOFF manifest key=%s images=%d expires_in=%ds",
                     manifest_key, len(urls), args.expires)

            if arms_mod.STORED_CLIP_ARM in arm_ids:
                arm_id = arm_ids[arms_mod.STORED_CLIP_ARM]
                copied = copy_stored_clip(conn, arm_id=arm_id,
                                          image_ids=sorted(stored))
                with conn.cursor() as cur:
                    cur.execute(_FINISH_ARM_SQL, {
                        "arm_id": arm_id, "status": "ok",
                        "dim": arms_mod.STORED_CLIP_DIM,
                        "note": f"copied {copied}/{len(stored)} stored vectors from "
                                f"image_clip_embeddings ({arms_mod.STORED_CLIP_MODEL}); "
                                "no GPU, no cost",
                    })
                LOG.info("BAKEOFF %s copied=%d/%d (an image with no incumbent vector "
                         "simply has no row on this arm)",
                         arms_mod.STORED_CLIP_ARM, copied, len(stored))
        except Exception as exc:  # noqa: BLE001 - the run row must not be left lying
            with conn.cursor() as cur:
                cur.execute(_FAIL_RUN_SQL,
                            {"run_id": run_id, "note": f"manifest stage failed: {exc}"})
            raise

    LOG.info("BAKEOFF done run_id=%d — next: stage=embed with run_id=%d", run_id, run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
