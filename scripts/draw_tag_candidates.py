"""Top up per-tag candidate queues by CLIP centroid retrieval (migration 449).

For each requested tag: skip it when its OPEN queue already reaches --target,
skip it when it has fewer than MIN_VERIFIED_POSITIVES human-verified positives
(no meaningful centroid), otherwise draw --count candidates across the category
mix. Resumable with no ledger of its own — the stored candidate rows ARE the
marker — and bounded by --count, --target and --max-seconds.

    python -m scripts.draw_tag_candidates --tag-id 12 --count 120
    python -m scripts.draw_tag_candidates --all-ready --target 200 --max-seconds 1800

Dispatch-free on purpose: unlike clip_tag.yml this job needs no GPU, no torch, no
R2 and no model download — it is pure SQL against the database, so a runner gives
it nothing the operator's own terminal does not, and its natural trigger ("I am
labeling this tag right now") is the sync API button. Ship a workflow when a
scheduled top-up is actually wanted.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

LOG = logging.getLogger("draw_tag_candidates")

# Emptiest OPEN queue first — the tag most starved of review work is the one worth
# drawing for. --all-ready takes every ACTIVE tag (the floor check is applied per
# tag below, so a tag that cannot be drawn is reported rather than silently
# missing); explicit --tag-id reaches an inactive tag too. Bounded by
# tag_taxonomy's ~51 rows either way.
_TAG_QUEUE_SQL = """
    SELECT t.id, t.label,
           COALESCE(q.open_count, 0)::int AS open_count
    FROM tag_taxonomy t
    LEFT JOIN (
      SELECT c.tag_id,
             count(*) FILTER (WHERE lab.image_id IS NULL) AS open_count
      FROM tag_candidates c
      LEFT JOIN image_tag_labels lab
        ON lab.image_id = c.image_id AND lab.tag_id = c.tag_id
      GROUP BY c.tag_id
    ) q ON q.tag_id = t.id
    WHERE (%(all_ready)s::boolean AND t.active)
       OR (NOT %(all_ready)s::boolean AND t.id = ANY(%(tag_ids)s::bigint[]))
    ORDER BY COALESCE(q.open_count, 0), t.id
"""


def _tag_rows(
    conn: Any, *, all_ready: bool, tag_ids: list[int],
) -> list[tuple[int, str, int]]:
    with conn.cursor() as cur:
        cur.execute(_TAG_QUEUE_SQL, {"all_ready": all_ready, "tag_ids": tag_ids})
        return [(int(r[0]), r[1], int(r[2])) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag-id", type=int, action="append", default=None,
                    help="Repeatable; explicit tags. Omit with --all-ready.")
    ap.add_argument("--all-ready", action="store_true",
                    help="Every ACTIVE tag with enough human-verified positives, "
                         "emptiest open queue first.")
    ap.add_argument("--target", type=int, default=None,
                    help="Skip a tag whose OPEN queue already reaches this "
                         "(default: tag_candidates.DEFAULT_OPEN_TARGET).")
    ap.add_argument("--count", type=int, default=None, help="Candidates per draw.")
    ap.add_argument("--category", default=None,
                    help="Scope every draw to one listings.category_main bucket.")
    ap.add_argument("--max-seconds", type=int, default=0,
                    help="Wall-clock budget across tags; finalize cleanly when "
                         "reached (0 = no budget).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report the tag list, open counts and floor status; write nothing.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
    )

    from scraper import db
    from toolkit import tag_candidates as tc

    target = tc.DEFAULT_OPEN_TARGET if args.target is None else args.target
    count = tc.DEFAULT_DRAW_COUNT if args.count is None else args.count
    if not args.tag_id and not args.all_ready:
        LOG.error("TAGCAND nothing to do: pass --tag-id or --all-ready")
        return 1
    if args.category is not None and args.category not in tc.CATEGORY_MIX:
        LOG.error("TAGCAND unknown --category %s (known: %s)",
                  args.category, ", ".join(tc.CATEGORY_MIX))
        return 1

    started = time.monotonic()
    tags_drawn = inserted = skipped = timeouts = errors = 0
    with db.connect() as conn:
        rows = _tag_rows(
            conn, all_ready=bool(args.all_ready), tag_ids=list(args.tag_id or []),
        )
        LOG.info("TAGCAND config tags=%d count=%d target=%d category=%s dry_run=%s",
                 len(rows), count, target, args.category, args.dry_run)
        for tag_id, label, open_count in rows:
            if args.max_seconds > 0 and time.monotonic() - started >= args.max_seconds:
                LOG.info("TAGCAND time budget %ds reached; finalizing cleanly",
                         args.max_seconds)
                break
            if open_count >= target:
                skipped += 1
                LOG.info("TAGCAND tag=%d label=%r status=skipped_target open=%d target=%d",
                         tag_id, label, open_count, target)
                continue
            verified = tc.count_verified_positives(conn, tag_id=tag_id)
            if verified < tc.MIN_VERIFIED_POSITIVES:
                skipped += 1
                LOG.info(
                    "TAGCAND tag=%d label=%r status=insufficient_positives "
                    "verified=%d floor=%d",
                    tag_id, label, verified, tc.MIN_VERIFIED_POSITIVES,
                )
                continue
            if args.dry_run:
                LOG.info("TAGCAND tag=%d label=%r status=would_draw open=%d verified=%d",
                         tag_id, label, open_count, verified)
                continue
            tag_started = time.monotonic()
            try:
                # max_seconds=0: the 45s default is shaped for a synchronous admin
                # request, and inheriting it here would make every tag of an
                # --all-ready run drop its largest category quota. The run is
                # bounded between tags by --max-seconds, and each category is still
                # bounded by DRAW_STATEMENT_TIMEOUT_MS.
                res = tc.draw_candidates(
                    conn, tag_id=tag_id, count=count, category_main=args.category,
                    drawn_by="runner", max_seconds=0,
                )
            except Exception as exc:  # noqa: BLE001 - one tag must not kill the run
                errors += 1
                LOG.warning("TAGCAND tag=%d label=%r error=%s", tag_id, label, exc)
                continue
            tags_drawn += 1
            inserted += res["inserted"]
            timeouts += sum(1 for c in res["categories"] if c["status"] == "timeout")
            LOG.info(
                "TAGCAND tag=%d label=%r status=%s requested=%d inserted=%d "
                "near_dup=%d cap=%d pool=%d elapsed=%.1fs",
                tag_id, label, res["status"], res["requested"], res["inserted"],
                res["dropped_near_dup"], res["dropped_property_cap"],
                sum(c["pool_size"] for c in res["categories"]),
                time.monotonic() - tag_started,
            )
    LOG.info("TAGCAND done tags=%d inserted=%d skipped=%d timeouts=%d errors=%d",
             tags_drawn, inserted, skipped, timeouts, errors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
