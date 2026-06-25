"""One-shot corrective: re-singletonize apartment properties whose members span
an AREA gap the post-PR-#598 engine would now reject.

The pre-#598 engine merged byt listings up to a 20% area gap, and pHash on shared
development RENDER photos then chained adjacent area-bands into one property via
transitivity ("Rezidence Na Bradle": 73/87/99 m²; "Budovatelů": 59/62/74 m² bridged
by a NULL-floor listing). PR #598 unified the candidate area-gap reject to 10% and
added the floor-plan / distinctive-room gates, so the engine would NEVER form these
groupings now. This finds every *active* byt property whose active members' area
ratio exceeds `--min-ratio` and re-singletonizes it via
`toolkit.property_identity.split_property_to_singletons` — the representative child
stays, every other child detaches onto its own fresh singleton property.

Nothing is deleted (rule #3); the dedup engine re-merges any legitimate same-unit
pairs on its next daily run (a genuine cross-portal same-flat pair re-merges via
exact-address / pHash / visual). Operator-merged properties are EXCLUDED — we never
undo a human decision.

SCOPE / SAFETY. `--min-ratio` defaults to 1.25 (~>20% gap) — those bands are
different units, so the split is safe and the engine won't re-chain them. A ratio
near 1.11 (~>10%, matching the engine's reject) reaches the GRAY ZONE where a genuine
same-flat pair with area-measurement noise (gross vs net, balcony) would be split and,
because 10% is now a hard pre-pHash reject, NOT re-merged. Run the gray zone only with
operator awareness.

Runnable as `python -m scripts.resweep_incoherent_byt_properties` (dry-run by default;
pass `--apply` to mutate). Required env: SUPABASE_DB_URL.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from toolkit.property_identity import MergeError, split_property_to_singletons

LOG = logging.getLogger("resweep_incoherent")

# Active byt properties whose active children's max/min usable-area ratio exceeds the
# threshold, EXCLUDING any property an operator ever merged (never undo a human merge).
# Ordered worst-first so --limit takes the most egregious bands.
_FIND_SQL = """
    WITH byt_props AS (
        SELECT l.property_id AS id,
               count(*) AS n,
               min(l.area_m2) AS amin,
               max(l.area_m2) AS amax,
               array_agg(l.area_m2 ORDER BY l.area_m2) AS areas
        FROM listings l
        JOIN properties p ON p.id = l.property_id AND p.status = 'active'
        WHERE l.category_main = 'byt' AND l.is_active
              AND l.area_m2 IS NOT NULL AND l.area_m2 > 0
        GROUP BY l.property_id
        HAVING count(*) >= 2 AND max(l.area_m2) > %(min_ratio)s * min(l.area_m2)
    )
    SELECT bp.id, bp.n, bp.amin, bp.amax, bp.areas
    FROM byt_props bp
    WHERE bp.id NOT IN (
        SELECT survivor_property_id FROM property_merge_events
        WHERE source = 'operator' AND undone_at IS NULL
    )
    ORDER BY bp.amax / bp.amin DESC
"""


def _find(conn: Any, min_ratio: float) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_FIND_SQL, {"min_ratio": min_ratio})
        rows = cur.fetchall()
    return [
        {"id": int(r[0]), "n": int(r[1]), "amin": float(r[2]), "amax": float(r[3]),
         "areas": [float(a) for a in r[4]]}
        for r in rows
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually re-singletonize. Default is a dry-run report.")
    parser.add_argument("--min-ratio", type=float, default=1.25,
                        help="max/min area ratio threshold (default 1.25 ~ >20%% gap, safe; "
                             "1.111 ~ >10%% reaches the gray zone).")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap properties processed, worst-first (0 = no cap).")
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
        found = _find(conn, args.min_ratio)
        if args.limit:
            found = found[: args.limit]

        LOG.info(
            "RESWEEP found=%d min_ratio=%.3f member_listings=%d apply=%s",
            len(found), args.min_ratio, sum(f["n"] for f in found), args.apply,
        )

        if not args.apply:
            for f in found:
                LOG.info(
                    "RESWEEP would-split property=%d children=%d areas=%s ratio=%.2f",
                    f["id"], f["n"], f["areas"], f["amax"] / f["amin"],
                )
            LOG.info("RESWEEP dry-run; pass --apply to execute. exit")
            return 0

        split = detached = created = errors = 0
        for f in found:
            try:
                res = split_property_to_singletons(conn, property_id=f["id"])
            except MergeError as exc:
                errors += 1
                LOG.warning("RESWEEP skip property=%d: %s", f["id"], exc)
                continue
            d = res["data"]
            n_detached = len(d["detached_listing_ids"])
            if n_detached:
                split += 1
                detached += n_detached
                created += len(d["new_property_ids"])
                LOG.info(
                    "RESWEEP property=%d anchor=%s detached=%d new_properties=%s",
                    f["id"], d["anchor_listing_id"], n_detached, d["new_property_ids"],
                )

        LOG.info(
            "RESWEEP done split=%d detached_listings=%d new_properties=%d errors=%d",
            split, detached, created, errors,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
