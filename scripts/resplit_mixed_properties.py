"""One-shot corrective: dissolve legacy mixed-category property groupings.

The old geo-proximity matcher (replaced by the street+disposition dedup engine,
rule #15) sometimes merged categorically-different listings that sit at the same
coordinates — a flat with a house, a sale with a rental, a flat with a
commercial unit. The dedup engine + the `merge_properties` chokepoint guard now
refuse such merges (PR #412), but pre-guard groupings persist in `properties`.

This finds every *active* property whose child listings span more than one
`category_type` or `category_main` and re-singletonizes it via
`toolkit.property_identity.split_property_to_singletons` — the representative
child stays, every other child detaches onto its own fresh singleton property.
Nothing is deleted (rule #3); the dedup engine re-merges any legitimate
same-category pairs on its next daily run.

Runnable as `python -m scripts.resplit_mixed_properties` (dry-run by default;
pass `--apply` to mutate). Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from toolkit.property_identity import MergeError, split_property_to_singletons

LOG = logging.getLogger("resplit_mixed")

# Active properties whose children disagree on category_type or category_main.
_FIND_MIXED_SQL = """
    SELECT p.id,
           count(*)                                                          AS n,
           count(DISTINCT l.category_type) FILTER (WHERE l.category_type IS NOT NULL) AS distinct_ct,
           count(DISTINCT l.category_main) FILTER (WHERE l.category_main IS NOT NULL) AS distinct_cm,
           array_agg(DISTINCT l.category_type) AS cts,
           array_agg(DISTINCT l.category_main) AS cms
    FROM properties p
    JOIN listings l ON l.property_id = p.id
    WHERE p.status = 'active'
    GROUP BY p.id
    HAVING count(*) > 1 AND (
        count(DISTINCT l.category_type) FILTER (WHERE l.category_type IS NOT NULL) > 1
        OR count(DISTINCT l.category_main) FILTER (WHERE l.category_main IS NOT NULL) > 1
    )
    ORDER BY p.id
"""


def _find_mixed(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_FIND_MIXED_SQL)
        rows = cur.fetchall()
    return [
        {"id": int(r[0]), "n": int(r[1]), "distinct_ct": int(r[2]),
         "distinct_cm": int(r[3]), "cts": r[4], "cms": r[5]}
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually re-singletonize. Default is a dry-run report.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap properties processed (0 = no cap).")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        mixed = _find_mixed(conn)
        if args.limit:
            mixed = mixed[: args.limit]

        LOG.info(
            "RESPLIT found=%d mixed_type=%d mixed_main=%d apply=%s",
            len(mixed),
            sum(1 for m in mixed if m["distinct_ct"] > 1),
            sum(1 for m in mixed if m["distinct_cm"] > 1),
            args.apply,
        )

        if not args.apply:
            for m in mixed:
                LOG.info(
                    "RESPLIT would-split property=%d children=%d category_type=%s category_main=%s",
                    m["id"], m["n"], m["cts"], m["cms"],
                )
            LOG.info("RESPLIT dry-run; pass --apply to execute. exit")
            return 0

        split = detached = created = errors = 0
        for m in mixed:
            try:
                res = split_property_to_singletons(conn, property_id=m["id"])
            except MergeError as exc:
                errors += 1
                LOG.warning("RESPLIT skip property=%d: %s", m["id"], exc)
                continue
            d = res["data"]
            n_detached = len(d["detached_listing_ids"])
            if n_detached:
                split += 1
                detached += n_detached
                created += len(d["new_property_ids"])
                LOG.info(
                    "RESPLIT property=%d anchor=%s detached=%d new_properties=%s",
                    m["id"], d["anchor_listing_id"], n_detached, d["new_property_ids"],
                )

        LOG.info(
            "RESPLIT done split=%d detached_listings=%d new_properties=%d errors=%d",
            split, detached, created, errors,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
