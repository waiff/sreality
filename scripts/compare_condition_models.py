"""Read-only A/B: re-score already-scored listings with a candidate model.

Validates a cheaper condition-scoring model (e.g. claude-haiku-4-5)
against the production baseline (claude-sonnet-4-5) BEFORE flipping
`app_settings.llm_condition_model`. For each sampled listing that
already has a baseline score on its latest snapshot, it re-runs the
SAME scoring request through the candidate model and compares the two
building/apartment levels.

Read-only: it never writes `listing_condition_scores` and never touches
the `listings.*_condition_level` columns, so the Sonnet baseline is
preserved. The candidate calls DO log to `llm_calls` (that's the point —
real candidate-model cost), bounded by --limit and --max-cost-usd.

Usage (typically via .github/workflows/compare_condition_models.yml):

    python -m scripts.compare_condition_models \\
        --candidate-model claude-haiku-4-5 \\
        --limit 40 --max-cost-usd 2

Required env: SUPABASE_DB_URL, ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("compare_condition_models")


def _axis_stats(pairs: list[tuple[int, int]]) -> dict[str, Any]:
    """Agreement of one axis. `pairs` is [(baseline_level, candidate_level)]."""
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    diffs = [c - b for b, c in pairs]
    exact = sum(1 for d in diffs if d == 0)
    within1 = sum(1 for d in diffs if abs(d) <= 1)
    return {
        "n": n,
        "exact": exact,
        "exact_pct": round(100.0 * exact / n, 1),
        "within1": within1,
        "within1_pct": round(100.0 * within1 / n, 1),
        "mean_abs_diff": round(sum(abs(d) for d in diffs) / n, 3),
        "bias": round(sum(diffs) / n, 3),  # +ve = candidate scores higher
    }


def summarize_agreement(
    rows: list[tuple[int, int, int, int]],
) -> dict[str, Any]:
    """Per-axis agreement from [(b_base, b_cand, a_base, a_cand), ...]."""
    building = [(r[0], r[1]) for r in rows]
    apartment = [(r[2], r[3]) for r in rows]
    return {
        "n": len(rows),
        "building": _axis_stats(building),
        "apartment": _axis_stats(apartment),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline-model", default="claude-sonnet-4-5",
        help="Model whose existing scores are the comparison baseline.",
    )
    parser.add_argument(
        "--candidate-model", default="claude-haiku-4-5",
        help="Cheaper model to validate against the baseline.",
    )
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument(
        "--region-ids", default="",
        help="Comma-separated locality_region_id filter (empty = all).",
    )
    parser.add_argument("--n-images", type=int, default=0)
    parser.add_argument(
        "--max-cost-usd", type=float, default=2.0,
        help="Stop once the candidate-model spend for this run crosses this.",
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
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2

    region_ids = _parse_region_ids(args.region_ids)
    LOG.info(
        "COMPARE baseline=%s candidate=%s limit=%d region_ids=%s "
        "n_images=%d max_cost_usd=%.2f",
        args.baseline_model, args.candidate_model, args.limit,
        region_ids or "ALL", args.n_images, args.max_cost_usd,
    )

    import psycopg

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from toolkit.condition_scoring import (
        ScoringError,
        build_scoring_request,
        extract_condition_tool_call,
        resolve_snapshot,
    )

    with psycopg.connect(
        db_url, autocommit=True, prepare_threshold=None,
    ) as conn:
        sample = _select_baseline_scored(
            conn,
            baseline_model=args.baseline_model,
            region_ids=region_ids,
            limit=args.limit,
        )
        LOG.info("COMPARE sample=%d", len(sample))
        if not sample:
            LOG.warning(
                "COMPARE no baseline scores found for model=%s — nothing to do",
                args.baseline_model,
            )
            return 0

        baseline_avg = _baseline_avg_cost(conn, args.baseline_model)
        llm_client = LLMClient(conn, providers={"anthropic": AnthropicProvider()})

        rows: list[tuple[int, int, int, int]] = []
        disagreements: list[dict[str, Any]] = []
        candidate_cost = 0.0
        errors = 0

        for i, item in enumerate(sample, start=1):
            if candidate_cost >= args.max_cost_usd:
                LOG.warning(
                    "COMPARE cost cap hit cost=$%.4f cap=$%.2f stopping at %d/%d",
                    candidate_cost, args.max_cost_usd, i - 1, len(sample),
                )
                break
            sid = item["sreality_id"]
            snap = resolve_snapshot(conn, sid, item["snapshot_id"])
            if snap is None:
                errors += 1
                continue
            try:
                req = build_scoring_request(
                    conn, llm_client,
                    sreality_id=sid, snapshot=snap, n_images=args.n_images,
                )
                resp = llm_client.call(
                    called_for="score_listing_condition",
                    messages=req["messages"],
                    system=req["system"],
                    tools=req["tools"],
                    model=args.candidate_model,
                )
                parsed = extract_condition_tool_call(resp.tool_calls)
            except (ScoringError, Exception) as exc:  # noqa: BLE001
                errors += 1
                LOG.warning("COMPARE id=%d skipped error=%s", sid, exc)
                continue

            candidate_cost += float(resp.cost_usd or 0.0)
            b_base, a_base = item["building_level"], item["apartment_level"]
            b_cand, a_cand = parsed["building_level"], parsed["apartment_level"]
            rows.append((b_base, b_cand, a_base, a_cand))
            if abs(b_cand - b_base) >= 2 or abs(a_cand - a_base) >= 2:
                disagreements.append({
                    "sreality_id": sid,
                    "building": f"{b_base}->{b_cand}",
                    "apartment": f"{a_base}->{a_cand}",
                })
            if i % 10 == 0 or i == len(sample):
                LOG.info(
                    "COMPARE progress=%d/%d compared=%d errors=%d cost=$%.4f",
                    i, len(sample), len(rows), errors, candidate_cost,
                )

    summary = summarize_agreement(rows)
    cand_avg = candidate_cost / len(rows) if rows else 0.0
    _print_report(
        summary=summary,
        baseline_model=args.baseline_model,
        candidate_model=args.candidate_model,
        baseline_avg=baseline_avg,
        candidate_avg=cand_avg,
        candidate_cost=candidate_cost,
        errors=errors,
        disagreements=disagreements,
    )
    return 0


def _print_report(
    *,
    summary: dict[str, Any],
    baseline_model: str,
    candidate_model: str,
    baseline_avg: float | None,
    candidate_avg: float,
    candidate_cost: float,
    errors: int,
    disagreements: list[dict[str, Any]],
) -> None:
    def fmt(axis: dict[str, Any]) -> str:
        if not axis.get("n"):
            return "no data"
        return (
            f"exact {axis['exact_pct']}%  "
            f"within±1 {axis['within1_pct']}%  "
            f"MAD {axis['mean_abs_diff']}  "
            f"bias {axis['bias']:+}"
        )

    print("\n" + "=" * 64)
    print(f"CONDITION MODEL A/B  baseline={baseline_model}  candidate={candidate_model}")
    print("=" * 64)
    print(f"compared={summary['n']}  errors={errors}")
    print(f"  building  : {fmt(summary['building'])}")
    print(f"  apartment : {fmt(summary['apartment'])}")
    print("-" * 64)
    print(f"candidate spend this run : ${candidate_cost:.4f}  (${candidate_avg:.5f}/call)")
    if baseline_avg:
        ratio = candidate_avg / baseline_avg if baseline_avg else 0.0
        print(f"baseline  ${baseline_avg:.5f}/call (last 30d)  ->  candidate is {ratio:.2f}x")
        print(f"projected saving        : {(1 - ratio) * 100:.0f}% per scored listing")
    if disagreements:
        print("-" * 64)
        print(f"hard disagreements (|Δ| >= 2 on either axis): {len(disagreements)}")
        for d in disagreements[:20]:
            print(f"  id={d['sreality_id']}  building {d['building']}  apartment {d['apartment']}")
    print("=" * 64 + "\n")


def _parse_region_ids(raw: str) -> list[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            print(f"ERROR: --region-ids non-integer entry: {part!r}", file=sys.stderr)
            sys.exit(2)
    return out


def _select_baseline_scored(
    conn: Any,
    *,
    baseline_model: str,
    region_ids: list[int],
    limit: int,
) -> list[dict[str, Any]]:
    """Listings whose LATEST snapshot has a baseline-model score row.

    The cache stores the dated response model (e.g.
    `claude-sonnet-4-5-20250929`), so match the alias OR the alias plus a
    `-YYYYMMDD` snapshot suffix rather than exact-equals.
    """
    like_pattern = baseline_model + "-%"
    sql = (
        "WITH latest_snapshot AS ( "
        "  SELECT sreality_id, MAX(id) AS snapshot_id "
        "  FROM listing_snapshots GROUP BY sreality_id "
        ") "
        "SELECT cs.sreality_id, cs.snapshot_id, "
        "       cs.building_level, cs.apartment_level "
        "FROM listing_condition_scores cs "
        "JOIN latest_snapshot ls "
        "  ON ls.sreality_id = cs.sreality_id "
        " AND ls.snapshot_id = cs.snapshot_id "
        "JOIN listings l ON l.sreality_id = cs.sreality_id "
        "WHERE ( cs.model = %s OR cs.model LIKE %s ) "
        "  AND l.is_active = true "
        "  AND ( cardinality(%s::int[]) = 0 "
        "        OR l.locality_region_id = ANY(%s::int[]) ) "
        "ORDER BY cs.created_at DESC "
        "LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (baseline_model, like_pattern, region_ids, region_ids, limit))
        return [
            {
                "sreality_id": int(r[0]),
                "snapshot_id": int(r[1]),
                "building_level": int(r[2]),
                "apartment_level": int(r[3]),
            }
            for r in cur.fetchall()
        ]


def _baseline_avg_cost(conn: Any, baseline_model: str) -> float | None:
    sql = (
        "SELECT AVG(cost_usd) FROM llm_calls "
        "WHERE called_for = 'score_listing_condition' "
        "  AND model = %s "
        "  AND called_at > now() - interval '30 days' "
        "  AND cost_usd > 0"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (baseline_model,))
        row = cur.fetchone()
    return float(row[0]) if row and row[0] is not None else None


if __name__ == "__main__":
    sys.exit(main())
