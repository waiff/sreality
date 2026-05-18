"""Phase A curator — apply the whitelist rule to the raw marker dictionary.

The raw `data/condition_markers_v1.json` (output of
`scripts/aggregate_condition_markers.py`) contains every cluster the
aggregator found — including singletons, structure-type leaks
("cihlová stavba", "cihlový dům" — already covered by
`listings.building_type`), and amenity leaks ("sprchový kout",
"plovoucí podlahy"). Feeding that whole pool into the rubric pass /
scorer would bloat the prompt and confuse the model.

This script applies a deterministic whitelist:

    keep iff count >= 10 OR (level_hint == "high" AND sentiment != "neutral")

The two arms catch different signal types:
  - `count >= 10` keeps anything that's actually frequent in the
    sample, even if it's tagged medium or low.
  - The high+non-neutral arm rescues rare-but-critical markers:
    "k demolici" (7), "umakartové jádro" (7), "původní dřevěná
    okna" (6) — these need to be in the rubric even though they
    appear infrequently.

Marker IDs are preserved verbatim from the input so the rubric
references remain stable across re-runs.

No LLM, no new dependencies (rule #7). Stdlib only.

Usage:
    python -m scripts.curate_condition_markers \
        --input data/condition_markers_v1.json \
        --output data/condition_markers_curated.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

LOG = logging.getLogger("curate_condition_markers")


def passes_whitelist(cluster: dict[str, Any], *, min_count: int) -> bool:
    """Return True iff the cluster should be kept in the curated dictionary."""
    if cluster.get("count", 0) >= min_count:
        return True
    return (
        cluster.get("level_hint_majority") == "high"
        and cluster.get("sentiment_majority") != "neutral"
    )


def curate(
    raw: dict[str, Any], *, min_count: int = 10,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": raw.get("schema_version", 1),
        "source_total_extractions": raw.get("total_extractions", 0),
        "whitelist_rule": (
            f"count >= {min_count} OR "
            "(level_hint_majority == 'high' AND "
            "sentiment_majority != 'neutral')"
        ),
        "building": [
            c for c in raw.get("building", []) if passes_whitelist(c, min_count=min_count)
        ],
        "apartment": [
            c for c in raw.get("apartment", []) if passes_whitelist(c, min_count=min_count)
        ],
    }
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", default="data/condition_markers_v1.json",
        help="Path to the raw aggregator output.",
    )
    parser.add_argument(
        "--output", default="data/condition_markers_curated.json",
        help="Path to write the curated dictionary to.",
    )
    parser.add_argument(
        "--min-count", type=int, default=10,
        help="Frequency threshold for the curator's first arm (default 10).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    in_path = Path(args.input)
    if not in_path.is_file():
        print(f"ERROR: input {in_path} not found.", file=sys.stderr)
        return 2

    raw = json.loads(in_path.read_text(encoding="utf-8"))
    curated = curate(raw, min_count=args.min_count)

    LOG.info(
        "curated building=%d/%d apartment=%d/%d "
        "(rule: %s)",
        len(curated["building"]), len(raw.get("building", [])),
        len(curated["apartment"]), len(raw.get("apartment", [])),
        curated["whitelist_rule"],
    )

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(curated, ensure_ascii=False, indent=2, sort_keys=False),
        encoding="utf-8",
    )
    LOG.info("wrote %s", out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
