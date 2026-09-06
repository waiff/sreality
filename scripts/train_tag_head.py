"""Train ONE binary tag head on the frozen DINOv3 embeddings, and write its artifact.

    python -m scripts.train_tag_head --tag-id 19 --out data/tag_heads/tag-19.json
    python -m scripts.train_tag_head --tag-id 2 --dry-run

The tag is an argument. There is no built-in list of "the target tags" anywhere in
this lane: Gate 1's list is the operator's, it is not final, and a head is a
per-tag object regardless of how that list eventually reads. Run it once per tag.

Encoder: with no --model/--revision/... the run reads whichever identity has the
most rows in image_dinov3_embeddings and PRINTS it; the seven facts are written
into the artifact either way, so a head can never be silently scored against
vectors from a different encoder.

Needs `pip install -e ".[training]"` (scikit-learn) and, for --diagnostics,
`".[analysis]"` (faiss-cpu). Neither belongs in a runtime image; scoring an
embedding from a written artifact needs neither.

THE TRAINING SET IS THE OPERATOR'S. This reads it through
toolkit.machine_labeling / toolkit.tag_holdout and nothing else, so the sealed
exam stays excluded and the population matches the review page exactly. Do not run
it before the operator says the set is finalized.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from toolkit import tag_heads as th

LOG = logging.getLogger("train_tag_head")


def _encoder_from_args(args: argparse.Namespace) -> th.EncoderIdentity | None:
    given = {f: getattr(args, f) for f in th.ENCODER_FIELDS}
    if all(v is None for v in given.values()):
        return None
    missing = [f for f, v in given.items() if v is None]
    if missing:
        raise SystemExit(
            "encoder identity is all-or-nothing; missing --" + ", --".join(missing))
    return th.EncoderIdentity.from_dict(given)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag-id", type=int, required=True, help="tag_taxonomy.id to train.")
    ap.add_argument("--out", default=None, help="Artifact path (omit with --dry-run).")
    ap.add_argument("--n-splits", type=int, default=th.DEFAULT_N_SPLITS,
                    help="Grouped CV folds (capped by the groups available).")
    ap.add_argument("--C", type=float, default=th.DEFAULT_C, help="Inverse L2 strength.")
    ap.add_argument("--threshold", type=float, default=th.DEFAULT_THRESHOLD)
    ap.add_argument("--seed", type=int, default=th.DEFAULT_SEED)
    ap.add_argument("--diagnostics", action="store_true",
                    help="Nearest-neighbor analysis of the out-of-fold mistakes "
                         "(read-only; influences no number above).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Assemble and report the population; train nothing, write nothing.")
    for field in th.ENCODER_FIELDS:
        ap.add_argument(f"--{field}", default=None,
                        help="Encoder identity (all seven together, or none).")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from scraper import db

    encoder = _encoder_from_args(args)
    with db.connect() as conn:
        snap = th.assemble_dataset(conn, tag_id=args.tag_id, encoder=encoder)

    LOG.info("HEAD tag=%d encoder=%s", snap.tag_id,
             json.dumps(snap.encoder.as_dict(), sort_keys=True))
    LOG.info("HEAD dataset hash=%s pos=%d neg=%d groups=%d dim=%d",
             snap.dataset_hash, snap.n_positive, snap.n_negative,
             snap.n_groups, snap.dimension)
    if snap.missing_embedding:
        LOG.warning("HEAD %d labeled images have no vector under this encoder: %s",
                    len(snap.missing_embedding),
                    ",".join(str(i) for i in snap.missing_embedding[:20]))
    if snap.missing_group:
        LOG.warning("HEAD %d images have no listing_id; each got a singleton group",
                    len(snap.missing_group))
    if snap.conflicting:
        LOG.warning("HEAD %d images are labeled both positive and negative and were "
                    "dropped: %s", len(snap.conflicting),
                    ",".join(str(i) for i in snap.conflicting[:20]))
    if args.dry_run:
        return 0
    if not args.out:
        LOG.error("HEAD --out is required unless --dry-run")
        return 1

    head = th.train_head(
        snap, trained_at=datetime.now(timezone.utc), n_splits=args.n_splits,
        C=args.C, threshold=args.threshold, seed=args.seed)
    m = head.metrics
    LOG.info("HEAD graded n=%d (pos=%d neg=%d) over %d %s folds: "
             "precision=%.3f recall=%.3f f1=%.3f accuracy=%.3f",
             m.graded_n, m.graded_positive_n, m.graded_negative_n, m.n_splits,
             m.strategy, m.precision, m.recall, m.f1, m.accuracy)
    LOG.info("HEAD confusion tp=%d fp=%d tn=%d fn=%d",
             m.true_positives, m.false_positives, m.true_negatives, m.false_negatives)
    if m.graded_n < 100:
        LOG.warning("HEAD graded n=%d is small — read the precision as an estimate "
                    "with a wide interval, not as a measurement", m.graded_n)

    path = th.save_artifact(head.artifact, args.out)
    LOG.info("HEAD wrote %s", path)

    if args.diagnostics:
        from toolkit import tag_heads_eval as te

        report = te.diagnose_head(head)
        LOG.info("HEAD diagnostics (read-only; not part of the numbers above):")
        for d in report.diagnostics:
            LOG.info("  %s image=%d (listing %d, score %.3f) nearest %s image=%d "
                     "cos=%.4f", d.kind, d.image_id, d.listing_id, d.score,
                     "positive" if d.neighbor_label == 1 else "negative",
                     d.neighbor_image_id, d.similarity)
    return 0


if __name__ == "__main__":
    sys.exit(main())
