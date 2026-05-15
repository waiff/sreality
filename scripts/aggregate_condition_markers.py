"""Phase A aggregator — turn `listing_marker_extractions` rows into a
deduplicated, frequency-ranked marker dictionary.

Reads every row from `listing_marker_extractions`, normalises each
marker text (lowercase, NFKD diacritic strip, whitespace collapse),
clusters near-duplicates with stdlib `difflib.SequenceMatcher`, and
writes the result to `data/condition_markers_v1.json` for operator
review.

No LLM, no new dependencies (rule #7). Stdlib only.

After the operator reviews the JSON, the level-rubric pass is a
separate one-shot LLM call (see `--rubric` flag). Today the rubric
pass is a placeholder that prints the dictionary back so the
operator can see what would be fed in; the actual `record_condition_rubric`
tool is wired up in Phase B alongside `score_listing_condition`.

Usage:
    python scripts/aggregate_condition_markers.py \
        --output data/condition_markers_v1.json \
        --min-cluster-count 3 \
        --similarity-threshold 0.85
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

LOG = logging.getLogger("aggregate_condition_markers")

_WS_RE = re.compile(r"\s+")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", default="data/condition_markers_v1.json",
        help="Where to write the dictionary JSON (default data/condition_markers_v1.json).",
    )
    parser.add_argument(
        "--min-cluster-count", type=int, default=3,
        help="Drop clusters whose total occurrence count is below this threshold (default 3).",
    )
    parser.add_argument(
        "--similarity-threshold", type=float, default=0.85,
        help="difflib.SequenceMatcher ratio; >= this merges two phrases into one cluster (default 0.85).",
    )
    parser.add_argument(
        "--token-jaccard-threshold", type=float, default=0.7,
        help="Token-set Jaccard; >= this also merges. Fires when ratio is borderline but tokens overlap (default 0.7).",
    )
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

    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        rows = _load_all_markers(conn)

    LOG.info("loaded %d raw marker entries", len(rows))
    if not rows:
        LOG.warning("no rows in listing_marker_extractions; nothing to aggregate")
        return 1

    building_raw = [r for r in rows if r["scope"] == "building"]
    apartment_raw = [r for r in rows if r["scope"] == "apartment"]

    building_clusters = cluster_markers(
        building_raw,
        similarity_threshold=args.similarity_threshold,
        token_jaccard_threshold=args.token_jaccard_threshold,
    )
    apartment_clusters = cluster_markers(
        apartment_raw,
        similarity_threshold=args.similarity_threshold,
        token_jaccard_threshold=args.token_jaccard_threshold,
    )

    building_clusters = [
        c for c in building_clusters if c["count"] >= args.min_cluster_count
    ]
    apartment_clusters = [
        c for c in apartment_clusters if c["count"] >= args.min_cluster_count
    ]

    _assign_ids(building_clusters, prefix="B")
    _assign_ids(apartment_clusters, prefix="A")

    output = {
        "schema_version": 1,
        "total_extractions": _count_distinct_extractions(rows),
        "min_cluster_count": args.min_cluster_count,
        "similarity_threshold": args.similarity_threshold,
        "token_jaccard_threshold": args.token_jaccard_threshold,
        "building": building_clusters,
        "apartment": apartment_clusters,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    LOG.info(
        "wrote %s (building=%d apartment=%d)",
        out_path, len(building_clusters), len(apartment_clusters),
    )
    return 0


def cluster_markers(
    rows: list[dict[str, Any]],
    *,
    similarity_threshold: float,
    token_jaccard_threshold: float,
) -> list[dict[str, Any]]:
    """Greedy single-pass clustering.

    For each row, compare its normalised key against every existing
    cluster's canonical normalised key. Merge into the first cluster
    that exceeds either the SequenceMatcher ratio threshold or the
    token-Jaccard threshold. Otherwise spawn a new cluster.

    Greedy gives O(N*K) where K is the number of distinct clusters —
    fine for the ~30k-row aggregate (2000 listings × ~15 markers avg).
    """
    clusters: list[dict[str, Any]] = []
    for row in rows:
        normalised = _normalise(row["marker_text"])
        if not normalised:
            continue
        matched = _find_cluster(
            clusters, normalised,
            similarity_threshold=similarity_threshold,
            token_jaccard_threshold=token_jaccard_threshold,
        )
        if matched is None:
            clusters.append({
                "canonical_normalised": normalised,
                "canonical_display": row["marker_text"].strip(),
                "count": 1,
                "variants": Counter([row["marker_text"].strip()]),
                "sentiment_counts": Counter([row["sentiment"]]),
                "level_hint_counts": Counter([row["suggested_level_implication"]]),
                "source_counts": Counter([row["source"]]),
                "examples": [row["evidence_quote"][:200]] if row.get("evidence_quote") else [],
            })
        else:
            matched["count"] += 1
            matched["variants"][row["marker_text"].strip()] += 1
            matched["sentiment_counts"][row["sentiment"]] += 1
            matched["level_hint_counts"][row["suggested_level_implication"]] += 1
            matched["source_counts"][row["source"]] += 1
            if len(matched["examples"]) < 5 and row.get("evidence_quote"):
                matched["examples"].append(row["evidence_quote"][:200])

    clusters.sort(key=lambda c: c["count"], reverse=True)
    return [_finalise_cluster(c) for c in clusters]


def _find_cluster(
    clusters: list[dict[str, Any]],
    normalised: str,
    *,
    similarity_threshold: float,
    token_jaccard_threshold: float,
) -> dict[str, Any] | None:
    tokens = set(normalised.split())
    for c in clusters:
        canonical = c["canonical_normalised"]
        if SequenceMatcher(None, canonical, normalised).ratio() >= similarity_threshold:
            return c
        c_tokens = set(canonical.split())
        if not tokens or not c_tokens:
            continue
        jaccard = len(tokens & c_tokens) / len(tokens | c_tokens)
        if jaccard >= token_jaccard_threshold:
            return c
    return None


def _finalise_cluster(c: dict[str, Any]) -> dict[str, Any]:
    most_common_variant = c["variants"].most_common(1)[0][0]
    return {
        "canonical": most_common_variant,
        "canonical_normalised": c["canonical_normalised"],
        "count": c["count"],
        "sentiment_majority": c["sentiment_counts"].most_common(1)[0][0],
        "sentiment_counts": dict(c["sentiment_counts"]),
        "level_hint_majority": c["level_hint_counts"].most_common(1)[0][0],
        "level_hint_counts": dict(c["level_hint_counts"]),
        "source_counts": dict(c["source_counts"]),
        "variants": [v for v, _ in c["variants"].most_common()],
        "examples": c["examples"],
    }


def _assign_ids(clusters: list[dict[str, Any]], *, prefix: str) -> None:
    for i, c in enumerate(clusters, start=1):
        c["marker_id"] = f"{prefix}{i:03d}"


def _load_all_markers(conn: Any) -> list[dict[str, Any]]:
    sql = (
        "SELECT sreality_id, snapshot_id, markers "
        "FROM listing_marker_extractions"
    )
    rows: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for sid, snap_id, markers in cur.fetchall():
            if not isinstance(markers, list):
                continue
            for m in markers:
                if not isinstance(m, dict):
                    continue
                if "marker_text" not in m or "scope" not in m:
                    continue
                rows.append({
                    "sreality_id": sid,
                    "snapshot_id": snap_id,
                    "marker_text": m.get("marker_text", ""),
                    "scope": m.get("scope"),
                    "evidence_quote": m.get("evidence_quote", ""),
                    "sentiment": m.get("sentiment", "neutral"),
                    "suggested_level_implication": m.get(
                        "suggested_level_implication", "low",
                    ),
                    "source": m.get("source", "text"),
                })
    return rows


def _count_distinct_extractions(rows: list[dict[str, Any]]) -> int:
    return len({(r["sreality_id"], r["snapshot_id"]) for r in rows})


def _normalise(text: str) -> str:
    if not text:
        return ""
    lowered = text.strip().lower()
    nfkd = unicodedata.normalize("NFKD", lowered)
    no_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    collapsed = _WS_RE.sub(" ", no_diacritics).strip()
    return collapsed


if __name__ == "__main__":
    sys.exit(main())
