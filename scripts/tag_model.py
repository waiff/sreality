"""The tag model lane: promote a bake-off cell, score images, activate a version.

    python -m scripts.tag_model status
    python -m scripts.tag_model promote --run-id 1 --arm dinov2-l14-reg@504/bf16 \
        --mode pos_neg --version v1 --label "run 1, dinov2-l14 @504" --dry-run
    python -m scripts.tag_model score --version v1 --source bakeoff --dry-run
    python -m scripts.tag_model activate --version v1

THE LOOP, and the reason it has four verbs instead of one (PROGRAM.md ledger
2026-09-09 (b)):

  * `promote` freezes ONE (run, arm, mode) cell of a bake-off into a versioned
    model with status `candidate`. It trains — it needs `pip install -e
    ".[training]"` — and it writes nothing anyone reads.
  * `score` fills that version's per-image store. Resumable, batched, `--limit`,
    `--dry-run`; pure-Python inference, no ML library.
  * `activate` makes exactly one version the one consumers read. Separate and
    explicit ON PURPOSE: a half-scored version must never be reachable, and
    rolling back is `activate` on the older version rather than a re-run.
  * `status` says what exists — every version, its head count, how many images it
    has scored, and which one is active.

Adding heads, changing the training set, changing the encoder: all the same move,
a NEW VERSION. The winner is an argmax over a head set, so it only means anything
over one set at a time; nothing is ever edited in place.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from toolkit import tag_head_bakeoff as bo
from toolkit import tag_heads as th
from toolkit import tag_models as tm

LOG = logging.getLogger("tag_model")


def _split_ids(raw: str | None) -> list[int]:
    """`--heads 11,12` and `--heads "11, 12"` are the same list.

    A workflow_dispatch input is ONE string, so a lane always hands this a single
    comma-joined token; the bake-off runner learned the same lesson the expensive
    way (PROGRAM.md 2026-09-08 (i))."""
    return [int(part.strip()) for part in (raw or "").split(",") if part.strip()]


def _report_status(conn, args: argparse.Namespace) -> int:
    models = tm.list_models(conn, limit=args.limit)
    if not models:
        LOG.info("TAGMODEL no model versions exist yet")
        return 0
    LOG.info("TAGMODEL %-10s %-10s %-24s %6s %10s  %s",
             "version", "status", "arm", "heads", "scored", "label")
    for model in models:
        LOG.info("TAGMODEL %-10s %-10s %-24s %6s %10s  %s",
                 model.version, model.status, model.source_arm or "-",
                 model.n_heads if model.n_heads is not None else "?",
                 model.n_scored if model.n_scored is not None else "?",
                 model.label)
    live = tm.active_model(conn)
    LOG.info("TAGMODEL active=%s", live.version if live else "NONE")
    if args.version:
        one = tm.get_model(conn, version=args.version)
        if one is None:
            LOG.error("TAGMODEL no version %r", args.version)
            return 1
        LOG.info("TAGMODEL %s encoder=%s mode=%s heads=%s dataset_hash=%s",
                 one.version, one.encoder.as_dict(), one.mode, list(one.heads),
                 one.dataset_hash)
        for head in tm.model_heads(conn, model_id=one.id):
            cv = head.metrics.get("cv", {})
            LOG.info("TAGMODEL   head %-6d %-30s f1=%s graded_n=%s",
                     head.tag_id, (head.label or "")[:30],
                     cv.get("f1"), cv.get("graded_n"))
    return 0


def _do_promote(conn, args: argparse.Namespace) -> int:
    if args.dry_run:
        run = None
        try:
            arms = [a.arm for a in bo.list_arms(conn, run_id=args.run_id)]
            run = bo.get_run(conn, run_id=args.run_id)
        except Exception as exc:                      # noqa: BLE001 - reported
            LOG.error("TAGMODEL cannot read run %d: %s", args.run_id, exc)
            return 1
        if run is None:
            LOG.error("TAGMODEL run %d does not exist", args.run_id)
            return 1
        heads = _split_ids(args.heads) or [int(h) for h in run.get("heads") or ()]
        LOG.info("TAGMODEL dry-run: would promote run=%d arm=%s mode=%s as %s",
                 args.run_id, args.arm, args.mode, args.version)
        LOG.info("TAGMODEL run arms: %s", ", ".join(arms) or "none")
        LOG.info("TAGMODEL heads (%d): %s", len(heads), heads)
        if args.arm not in arms:
            LOG.error("TAGMODEL arm %r is not one of this run's arms", args.arm)
            return 1
        if tm.get_model(conn, version=args.version) is not None:
            LOG.error("TAGMODEL version %r already exists", args.version)
            return 1
        return 0
    try:
        model, outcomes = tm.promote(
            conn, run_id=args.run_id, arm=args.arm, mode=args.mode,
            version=args.version, label=args.label or args.version,
            note=args.note or None, tag_ids=_split_ids(args.heads) or None,
            n_splits=args.n_splits, C=args.C, threshold=args.threshold,
            seed=args.seed)
    except tm.TagModelError as exc:
        LOG.error("TAGMODEL %s", exc)
        return 1
    ok = [o for o in outcomes if o.status == "ok"]
    LOG.info("TAGMODEL promoted %s (id=%d) status=%s heads=%d/%d arm=%s mode=%s",
             model.version, model.id, model.status, len(ok), len(outcomes),
             model.source_arm, model.mode)
    for outcome in outcomes:
        if outcome.status == "ok":
            cv = outcome.metrics.get("cv", {})
            # missing= is the count of labelled images this arm has no vector for:
            # they never reach the fit, and a silent drop looks like a bad head.
            LOG.info("TAGMODEL   head %-6d %-30s f1=%.3f graded_n=%d missing=%d%s",
                     outcome.tag_id, outcome.label[:30],
                     float(cv.get("f1", 0.0)), int(cv.get("graded_n", 0)),
                     int(outcome.metrics.get("n_missing_embedding", 0)),
                     "  [training set moved since the run — the copied bake-off "
                     "numbers describe the older fit]"
                     if outcome.metrics.get("bakeoff", {}).get("dataset_moved")
                     else "")
        else:
            LOG.warning("TAGMODEL   head %d %s NOT promoted: %s",
                        outcome.tag_id, outcome.label, outcome.note)
    LOG.info("TAGMODEL next: score it, then activate it — a candidate is read "
             "by nobody until `activate` runs")
    return 0


def _do_score(conn, args: argparse.Namespace) -> int:
    model = tm.get_model(conn, version=args.version)
    if model is None:
        LOG.error("TAGMODEL no model version %r", args.version)
        return 1
    try:
        source = tm.resolve_source(conn, model=model, spec=args.source)
    except tm.TagModelError as exc:
        LOG.error("TAGMODEL %s", exc)
        return 1
    try:
        report = tm.score(
            conn, model=model, source=source, batch=args.batch, limit=args.limit,
            force=args.force, dry_run=args.dry_run)
    except tm.TagModelError as exc:
        LOG.error("TAGMODEL %s", exc)
        return 1
    LOG.info("TAGMODEL score %s source=%s considered=%d written=%d "
             "already-scored=%d no-vector=%d%s",
             report.version, report.source, report.considered, report.written,
             report.skipped_scored, report.missing_vector,
             " (DRY RUN, nothing written)" if report.dry_run else "")
    if not args.dry_run:
        LOG.info("TAGMODEL %s now holds %d scored image(s)", model.version,
                 tm.scored_count(conn, model_id=model.id))
    return 0


def _do_activate(conn, args: argparse.Namespace) -> int:
    if args.dry_run:
        model = tm.get_model(conn, version=args.version)
        if model is None:
            LOG.error("TAGMODEL no model version %r", args.version)
            return 1
        live = tm.active_model(conn)
        LOG.info("TAGMODEL dry-run: would activate %s (%d scored image(s)); "
                 "current active=%s would be retired",
                 model.version, tm.scored_count(conn, model_id=model.id),
                 live.version if live else "NONE")
        return 0
    try:
        model = tm.activate(conn, version=args.version)
    except tm.TagModelError as exc:
        LOG.error("TAGMODEL %s", exc)
        return 1
    LOG.info("TAGMODEL %s is ACTIVE (id=%d, %d heads, %d scored image(s))",
             model.version, model.id, len(model.heads),
             tm.scored_count(conn, model_id=model.id))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Every version, and which is active.")
    status.add_argument("--version", default=None,
                        help="Also print this version's heads and their numbers.")
    status.add_argument("--limit", type=int, default=50)

    prom = sub.add_parser("promote", help="Freeze one bake-off cell as a candidate.")
    prom.add_argument("--run-id", type=int, required=True)
    prom.add_argument("--arm", required=True,
                      help="Arm name from that run (e.g. dinov2-l14-reg@504/bf16).")
    prom.add_argument("--mode", default=th.MODE_POS_NEG,
                      choices=list(th.MODES),
                      help="Training mode. pos_neg is the live one; "
                           "pos_only_free_neg (the 'borrowed no') and "
                           "pos_only_centroid (the 'closeness only') were retired "
                           "from the experiment on 2026-09-09 and stay reachable "
                           "only to reproduce a bake-off cell — a centroid head's "
                           "score is a cosine, not a calibrated probability. One "
                           "model is one mode, because the winner is an argmax and "
                           "mixed score scales cannot be compared.")
    prom.add_argument("--version", required=True, help="e.g. v1. Immutable.")
    prom.add_argument("--label", default=None, help="Short human name.")
    prom.add_argument("--note", default=None)
    prom.add_argument("--heads", default=None,
                      help="Comma-separated tag ids, overriding the run's frozen "
                           "head list.")
    prom.add_argument("--n-splits", type=int, default=th.DEFAULT_N_SPLITS)
    prom.add_argument("--C", type=float, default=th.DEFAULT_C)
    prom.add_argument("--threshold", type=float, default=th.DEFAULT_THRESHOLD)
    prom.add_argument("--seed", type=int, default=th.DEFAULT_SEED)
    prom.add_argument("--dry-run", action="store_true",
                      help="Report the run, its arms and the head list; train "
                           "nothing.")

    sc = sub.add_parser("score", help="Score images under one version.")
    sc.add_argument("--version", required=True)
    sc.add_argument("--source", default=tm.SOURCE_BAKEOFF_PREFIX,
                    help="'bakeoff' (the model's own run), 'bakeoff:<run_id>', or "
                         "'production' (image_dinov3_embeddings under the model's "
                         "seven identity facts).")
    sc.add_argument("--batch", type=int, default=tm.DEFAULT_BATCH)
    sc.add_argument("--limit", type=int, default=None,
                    help="Stop after this many images considered.")
    sc.add_argument("--force", action="store_true",
                    help="Re-score images this version already scored.")
    sc.add_argument("--dry-run", action="store_true")

    act = sub.add_parser("activate", help="Make one version the active one.")
    act.add_argument("--version", required=True)
    act.add_argument("--dry-run", action="store_true")
    return ap


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


_HANDLERS = {
    "status": _report_status,
    "promote": _do_promote,
    "score": _do_score,
    "activate": _do_activate,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from scraper import db

    with db.connect() as conn:
        return _HANDLERS[args.command](conn, args)


if __name__ == "__main__":
    sys.exit(main())
