"""Draw and seal the sealed exam cohort (migration 458).

    python -m scripts.draw_exam_cohort --cohort exam_v1 --pure-random 100
    python -m scripts.draw_exam_cohort --cohort gold_v1 --curated-per-tag 20
    python -m scripts.draw_exam_cohort --cohort exam_v1 --status
    python -m scripts.draw_exam_cohort --cohort exam_v1 --seal

Runs as .github/workflows/draw_exam_cohort.yml. Pure SQL — no model, no R2, no
LLM key; the stratified half is added by the screening lane, which is where the
money is spent.

The cohort is created on first use and stays OPEN until --seal. That is deliberate:
the stratified frame cannot be drawn before the screener has run, so an exam is
necessarily two writes. "One-way door" means a SEALED cohort is immutable, not that
a cohort takes one write.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

LOG = logging.getLogger("draw_exam_cohort")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cohort", required=True, help="Cohort name, e.g. exam_v1.")
    ap.add_argument("--pure-random", type=int, default=0,
                    help="Draw N uniformly at random from the embedded corpus.")
    ap.add_argument("--curated-per-tag", type=int, default=0,
                    help="Seat up to N of the operator's draft-marked images per "
                         "flagged tag in a CURATED cohort (migration 462) for "
                         "careful re-labeling. Creates the cohort with "
                         "purpose='curated' on first use.")
    ap.add_argument("--seal", action="store_true",
                    help="Close the cohort. Irreversible.")
    ap.add_argument("--status", action="store_true",
                    help="Report composition and write nothing.")
    ap.add_argument("--note", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would happen; write nothing.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
    )

    from scraper import db
    from toolkit import tag_definitions as td
    from toolkit import tag_exam, tag_holdout

    if args.pure_random < 0 or args.curated_per_tag < 0:
        LOG.error("EXAM counts must not be negative")
        return 1
    if sum(1 for x in (args.pure_random, args.curated_per_tag, args.seal,
                       args.status) if x) != 1:
        LOG.error("EXAM pass exactly one of --pure-random, --curated-per-tag, "
                  "--seal or --status")
        return 1
    # Sealing in the same breath as drawing would close the exam before the
    # stratified half could be added — the one ordering mistake that cannot be
    # undone.
    if args.seal and args.pure_random:
        LOG.error("EXAM refusing to draw and seal in one run: seal is irreversible "
                  "and the stratified frame is added by a later lane")
        return 1

    model = td.embedding_model()
    revision = td.embedding_revision()

    with db.connect() as conn:
        cohort = tag_holdout.get_cohort(conn, name=args.cohort)

        if args.status:
            if cohort is None:
                LOG.info("EXAM cohort=%r status=absent", args.cohort)
                return 0
            comp = tag_exam.composition(conn, cohort_id=cohort["id"])
            LOG.info("EXAM cohort=%r sealed=%s frame_size=%d model=%s rev=%s",
                     cohort["name"], cohort["sealed_at"] is not None,
                     cohort["frame_size"], cohort["model"],
                     (cohort["revision"] or "-")[:8])
            for c in comp:
                LOG.info("EXAM   frame=%-12s stratum=%-22s n=%-4d p=%.6g..%.6g",
                         c["frame"], c["stratum"], c["n"], c["p_min"], c["p_max"])
            LOG.info("EXAM   total=%d protected_images=%d",
                     sum(c["n"] for c in comp), tag_holdout.holdout_size(conn))
            return 0

        if args.dry_run:
            LOG.info("EXAM dry-run cohort=%r exists=%s pure_random=%d "
                     "curated_per_tag=%d seal=%s model=%s",
                     args.cohort, cohort is not None, args.pure_random,
                     args.curated_per_tag, args.seal, model)
            return 0

        if args.seal:
            if cohort is None:
                LOG.error("EXAM cannot seal %r: it does not exist", args.cohort)
                return 1
            out = tag_exam.seal_cohort(conn, cohort_id=cohort["id"])
            LOG.info("EXAM seal cohort=%r status=%s sealed_at=%s",
                     args.cohort, out["status"], out["sealed_at"])
            return 0

        purpose = "curated" if args.curated_per_tag else "holdout"
        if cohort is None:
            cohort = tag_exam.create_cohort(
                conn, name=args.cohort, model=model, revision=revision,
                note=args.note, purpose=purpose,
            )
            LOG.info("EXAM created cohort=%r id=%d purpose=%s frame_size=%d",
                     cohort["name"], cohort["id"], cohort["purpose"],
                     cohort["frame_size"])
        elif cohort["sealed_at"] is not None:
            LOG.error("EXAM cohort=%r is sealed; it cannot take members", args.cohort)
            return 1

        if args.curated_per_tag:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM tag_taxonomy "
                    "WHERE routing_categories IS NOT NULL AND active ORDER BY id")
                tag_ids = [int(r[0]) for r in cur.fetchall()]
            res = tag_exam.draw_curated_from_drafts(
                conn, cohort_id=cohort["id"], tag_ids=tag_ids,
                per_tag=args.curated_per_tag,
            )
            LOG.info("EXAM curated %s", json.dumps(res, default=str))
            for t, row in sorted(res["tags"].items()):
                LOG.info("EXAM   tag=%-4s draft_positives=%-4d seated=%d",
                         t, row["draft_positives"], row["seated"])
            return 0

        res = tag_exam.draw_pure_random(
            conn, cohort_id=cohort["id"], count=args.pure_random,
        )
        LOG.info("EXAM pure_random %s", json.dumps(res, default=str))
        if res["short_by"]:
            # Not an error: a short draw is an honest outcome. It IS a signal that
            # PROBE_FACTOR no longer suits the corpus, so it must be loud.
            LOG.warning("EXAM pure_random short by %d of %d — raise PROBE_FACTOR",
                        res["short_by"], res["requested"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
