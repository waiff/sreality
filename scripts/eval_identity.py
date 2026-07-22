"""Evaluate the dedup / property-identity matcher against the golden set (migration 223).

Reports, per category family and overall: how the matcher classifies each labeled pair
(auto_merge / candidate / reject / not_blocked) and the derived metrics that gate any
rule change — auto-merge PRECISION (the paramount number: a false auto-merge corrupts
history) and recall, plus the false-merge rate on the known-distinct negatives.

P0 baseline: scores the CURRENT engine — `toolkit.dedup_engine.classify_pair` over the
shared street block. Non-apartment families show ~0 recall here (they are not blocked /
get rejected on the absent disposition), which is precisely the coverage gap this work
closes; a later run measures the category-aware matcher against the same set.

Run: `python -m scripts.eval_identity` (env: SUPABASE_DB_URL). Pure metrics live in
`summarize()` so they are unit-tested without a DB.
"""

from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from typing import Any

from toolkit.dedup_engine import ListingKey, classify_pair, street_group_keys

LOG = logging.getLogger("eval_identity")

# A prediction surfaces the pair to the operator (auto-merge or review queue) vs not.
_FOUND = {"auto_merge", "candidate"}


def _safe_div(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def summarize(observations: list[tuple[bool, str, str | None]]) -> dict[str, dict[str, Any]]:
    """Aggregate (is_same, predicted, category) tuples into per-category metrics.

    predicted ∈ {'auto_merge','candidate','reject','not_blocked'}. Pure — no DB.
    Returns {category | '__all__': {counts + precision/recall}}.
    """
    groups: dict[str, dict[str, Any]] = {}

    def _g(cat: str) -> dict[str, Any]:
        return groups.setdefault(cat, {
            "positives": 0, "negatives": 0,
            "tp_auto": 0, "fp_auto": 0, "tp_found": 0, "fp_found": 0,
            "pred": defaultdict(int),
        })

    def _record(g: dict[str, Any], is_same: bool, predicted: str) -> None:
        g["pred"][predicted] += 1
        if is_same:
            g["positives"] += 1
            if predicted == "auto_merge":
                g["tp_auto"] += 1
            if predicted in _FOUND:
                g["tp_found"] += 1
        else:
            g["negatives"] += 1
            if predicted == "auto_merge":
                g["fp_auto"] += 1
            if predicted in _FOUND:
                g["fp_found"] += 1

    for is_same, predicted, category in observations:
        _record(_g(category or "unknown"), is_same, predicted)
        _record(_g("__all__"), is_same, predicted)

    for g in groups.values():
        g["pred"] = dict(g["pred"])
        g["precision_auto"] = _safe_div(g["tp_auto"], g["tp_auto"] + g["fp_auto"])
        g["recall_auto"] = _safe_div(g["tp_auto"], g["positives"])
        g["recall_found"] = _safe_div(g["tp_found"], g["positives"])
        # The safety metric: fraction of KNOWN-DISTINCT pairs the matcher would auto-merge.
        g["false_merge_rate"] = _safe_div(g["fp_auto"], g["negatives"])
    return groups


_FEATURES_SQL = """
    SELECT sreality_id, source, street, street_id, disposition, house_number, floor,
           area_m2, left(description, 600) AS description, category_type, category_main,
           obec_id, id
    FROM listings
    WHERE sreality_id = ANY(%s)
"""


def _key_from_row(row: tuple[Any, ...], street_key: str) -> ListingKey:
    raw_street_id = int(row[3]) if row[3] is not None else None
    street_id = raw_street_id if raw_street_id is not None and raw_street_id > 0 else None
    listing_id = int(row[12])  # surrogate PK (listings.id): NOT-NULL, distinct per listing
    return ListingKey(
        # sreality_id is None-safe: post-Gate-2 a non-sreality row carries NULL here and
        # int(None) would crash. property_id/listing_id use the surrogate so two DISTINCT
        # NULL-sreality listings never collide — property_id==property_id -> already_merged,
        # listing_id==listing_id -> same_listing — and the eval never short-circuits a real pair.
        sreality_id=int(row[0]) if row[0] is not None else None,
        property_id=listing_id, listing_id=listing_id, source=row[1],
        street_key=street_key, disposition=row[4], house_number=row[5],
        floor=int(row[6]) if row[6] is not None else None,
        area_m2=float(row[7]) if row[7] is not None else None,
        description=row[8], category_type=row[9], category_main=row[10],
        street_id=street_id,
    )


def _predict(a: tuple[Any, ...], b: tuple[Any, ...]) -> str:
    """Current-engine verdict for a pair: the action classify_pair would return IF the
    two listings are blocked together (share a street key), else 'not_blocked'."""
    keys_a = set(street_group_keys(a[2], a[3], a[11]))
    keys_b = set(street_group_keys(b[2], b[3], b[11]))
    shared = keys_a & keys_b
    if not shared:
        return "not_blocked"
    sk = sorted(shared)[0]
    # property_id/listing_id are set to the surrogate in _key_from_row (distinct per
    # listing), so distinct listings never trip the already_merged / same_listing short-circuit.
    return classify_pair(_key_from_row(a, sk), _key_from_row(b, sk)).action


def evaluate(conn: Any) -> dict[str, dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT left_sreality_id, right_sreality_id, is_same, category_main "
            "FROM dedup_golden_pairs"
        )
        pairs = cur.fetchall()
        ids = sorted({int(p[0]) for p in pairs} | {int(p[1]) for p in pairs})
        cur.execute(_FEATURES_SQL, (ids,))
        feats = {int(r[0]): r for r in cur.fetchall()}

    observations: list[tuple[bool, str, str | None]] = []
    for lo, hi, is_same, category in pairs:
        a, b = feats.get(int(lo)), feats.get(int(hi))
        predicted = _predict(a, b) if a is not None and b is not None else "not_blocked"
        observations.append((bool(is_same), predicted, category))
    return summarize(observations)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2
    import psycopg

    with psycopg.connect(db_url, autocommit=True, prepare_threshold=None) as conn:
        groups = evaluate(conn)

    for cat in sorted(groups, key=lambda c: (c != "__all__", c)):
        g = groups[cat]
        LOG.info(
            "EVAL %-10s pos=%d neg=%d precision_auto=%s recall_auto=%s recall_found=%s "
            "false_merge_rate=%s pred=%s",
            cat, g["positives"], g["negatives"], g["precision_auto"], g["recall_auto"],
            g["recall_found"], g["false_merge_rate"], g["pred"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
