"""End-to-end smoke test for the Phase 7 slice 1 reasoning agent.

Manual invocation only. Runs the real agent loop against the real
Postgres + the real LLM provider (Anthropic or Gemini), and prints a
report of what happened. Designed to run from GitHub Actions where
SUPABASE_DB_URL + ANTHROPIC_API_KEY (and optionally GEMINI_API_KEY)
are wired up as secrets.

What it does:
1. Pick a real recent Prague apartment rental (or use --sreality-id).
2. Build the target spec from its DB row.
3. Run `run_agent_estimation` with the active `rental_estimator_v1`
   skill against the chosen provider.
4. Print: provider, model, iterations, stop_reason, total cost,
   estimate (with p25/p75 + confidence), warning count, and the
   first 5 trace steps.

Exit codes:
    0 — agent reached `record_estimate` (status=success)
    1 — agent halted (max_iterations / max_cost / timeout / error)
    2 — preflight failed (env var, target lookup, etc.)

WARNING: This *does* spend real LLM credits (typically <$0.10 per
run with claude-sonnet-4-5; ~$0.05 with gemini-2.5-pro). It also
writes one row to `estimation_runs` per invocation — those are
audit trail, not pollution, but if you're cost-sensitive, run
sparingly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

LOG = logging.getLogger("smoke_agent")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider", choices=["anthropic", "gemini"], default="anthropic",
    )
    parser.add_argument(
        "--sreality-id", type=int, default=None,
        help="Specific listing to estimate. Default: pick a recent Prague 2+kk.",
    )
    parser.add_argument(
        "--skill", default="rental_estimator_v1",
        help="Skill row name (default: rental_estimator_v1).",
    )
    parser.add_argument(
        "--radius-m", type=int, default=1000,
    )
    parser.add_argument(
        "--max-age-days", type=int, default=14,
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

    if args.provider == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 2
    if args.provider == "gemini" and not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY is not set.", file=sys.stderr)
        return 2

    import psycopg

    from api.agent import run_agent_estimation
    from api.dependencies import get_providers
    from api.estimation_runs import TraceRecorder
    from api.llm_client import LLMClient
    from api.skills import load_skill
    from toolkit.comparables import ComparableFilters, TargetSpec

    with psycopg.connect(db_url, prepare_threshold=None) as conn:
        target_row = _pick_target(conn, args.sreality_id)
        if target_row is None:
            print("ERROR: no target listing found.", file=sys.stderr)
            return 2
        _print_target(target_row)

        skill = load_skill(conn, args.skill)
        if args.provider not in skill.preferred_model:
            print(
                f"ERROR: skill {args.skill!r} has no preferred_model entry "
                f"for provider {args.provider!r}.",
                file=sys.stderr,
            )
            return 2
        print(
            f"\nSkill: {skill.name} "
            f"(model={skill.preferred_model[args.provider]}, "
            f"max_iter={skill.limits.max_iterations}, "
            f"max_cost=${skill.limits.max_cost_usd:.2f}, "
            f"timeout={skill.limits.wall_clock_timeout_s:.0f}s)"
        )

        providers = get_providers()
        llm_client = LLMClient(conn, providers=providers)

        target = TargetSpec(
            lat=float(target_row["lat"]),
            lng=float(target_row["lng"]),
            area_m2=float(target_row["area_m2"]) if target_row["area_m2"] else None,
            disposition=target_row["disposition"],
            exclude_ids=[int(target_row["sreality_id"])],
        )
        filters = ComparableFilters(
            radius_m=args.radius_m,
            max_age_days=args.max_age_days,
        )

        # Insert a smoke-test estimation_runs row so per-turn llm_calls
        # attribute cleanly. Status flips to terminal when the loop ends.
        run_id = _insert_smoke_run(conn, target_row, args)

        recorder = TraceRecorder()
        print(f"\n--- Running agent (estimation_runs.id = {run_id}) ---")
        try:
            result = run_agent_estimation(
                conn, sreality_client=_DummySrealityClient(),
                llm_client=llm_client,
                target=target, filters=filters,
                purchase_price_czk=None,
                skill=skill, provider=args.provider,
                recorder=recorder, estimation_run_id=run_id,
            )
        except Exception as exc:
            LOG.exception("agent raised: %s", exc)
            _finalise_smoke_run(conn, run_id, status="failed",
                                error_message=f"{type(exc).__name__}: {exc}"[:1000])
            return 1

    _print_result(result, recorder)
    _finalise_smoke_run_with_result(conn, run_id, result, recorder)

    return 0 if result.metadata.get("stop_reason") == "record_estimate" else 1


def _pick_target(conn: Any, sreality_id: int | None) -> dict[str, Any] | None:
    """Pick a real recent Prague 2+kk apartment rental (or the explicit id)."""
    sql = (
        "SELECT sreality_id, locality, disposition, area_m2, price_czk, "
        "ROUND(ST_Y(geom::geometry)::numeric, 5) AS lat, "
        "ROUND(ST_X(geom::geometry)::numeric, 5) AS lng "
        "FROM listings WHERE category_main = 'byt' AND category_type = 'pronajem' "
        "AND is_active = true AND last_seen_at > now() - interval '3 days' "
        "AND price_czk IS NOT NULL AND geom IS NOT NULL "
    )
    if sreality_id is not None:
        sql += "AND sreality_id = %s LIMIT 1"
        params = (sreality_id,)
    else:
        sql += (
            "AND disposition = '2+kk' AND area_m2 BETWEEN 55 AND 70 "
            "AND ST_DWithin(geom, "
            "ST_SetSRID(ST_MakePoint(14.43, 50.08), 4326)::geography, 1500) "
            "ORDER BY last_seen_at DESC LIMIT 1"
        )
        params = ()
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        cols = [d[0] for d in cur.description] if cur.description else []
    return dict(zip(cols, row)) if row else None


def _print_target(row: dict[str, Any]) -> None:
    print(
        f"Target: sreality_id={row['sreality_id']} "
        f"{row['disposition']} {row['area_m2']}m² @ {row['locality']!r} "
        f"asking={row['price_czk']} CZK/mo "
        f"({row['lat']}, {row['lng']})"
    )


def _insert_smoke_run(
    conn: Any, target_row: dict[str, Any], args: argparse.Namespace,
) -> int:
    """Insert one estimation_runs row in status='running' so the per-turn
    llm_calls have a parent to attribute against. Status flips to terminal
    in _finalise_smoke_run_with_result.
    """
    from psycopg.types.json import Jsonb
    spec = {
        "lat": float(target_row["lat"]),
        "lng": float(target_row["lng"]),
        "area_m2": float(target_row["area_m2"]),
        "disposition": target_row["disposition"],
        "source": "smoke",
    }
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO estimation_runs "
            "(source, mode, status, input_sreality_id, input_listing_id, "
            "input_spec, source_kind, parse_confidence, trace) "
            "VALUES ('api', 'agent', 'running', %s, "
            # Dual-write (migration 324): surrogate listings.id beside the legacy key.
            "(SELECT id FROM listings WHERE sreality_id = %s), %s, "
            "'sreality', 'high', %s) RETURNING id",
            (
                int(target_row["sreality_id"]),
                int(target_row["sreality_id"]),
                Jsonb(spec),
                Jsonb({"version": 1, "summary": "smoke seed",
                       "steps": []}),
            ),
        )
        row = cur.fetchone()
    return int(row[0])


def _finalise_smoke_run(
    conn: Any, run_id: int, *, status: str, error_message: str | None = None,
) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE estimation_runs SET status = %s, error_message = %s "
            "WHERE id = %s",
            (status, error_message, run_id),
        )


def _finalise_smoke_run_with_result(
    conn: Any, run_id: int, result: Any, recorder: Any,
) -> None:
    from psycopg.types.json import Jsonb
    d = result.data
    md = result.metadata
    status = "success" if md.get("stop_reason") == "record_estimate" else "failed"
    err = None if status == "success" else f"agent halted: {md.get('stop_reason')}"
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE estimation_runs SET "
            "status = %s, "
            "estimated_monthly_rent_czk = %s, "
            "rent_p25_czk = %s, "
            "rent_p75_czk = %s, "
            "confidence = %s, "
            "comparables_used = %s, "
            "trace = %s, "
            "warnings = %s, "
            "error_message = %s "
            "WHERE id = %s",
            (
                status,
                d.get("estimated_monthly_rent_czk"),
                d.get("rent_p25_czk"),
                d.get("rent_p75_czk"),
                d.get("confidence"),
                Jsonb(d.get("comparables_used") or []),
                Jsonb(recorder.to_dict(f"smoke {status}")),
                Jsonb(d.get("warnings") or []) if d.get("warnings") else None,
                err,
                run_id,
            ),
        )


def _print_result(result: Any, recorder: Any) -> None:
    d = result.data
    md = result.metadata
    print("\n--- Agent finished ---")
    print(f"provider:       {md.get('provider')}")
    print(f"skill:          {md.get('skill')}")
    print(f"stop_reason:    {md.get('stop_reason')}")
    print(f"iterations:     {md.get('iterations')}")
    print(f"total_cost_usd: ${md.get('total_cost_usd', 0):.4f}")
    print(f"estimate:       {_fmt(d.get('estimated_monthly_rent_czk'))} CZK/mo")
    print(f"p25 / p75:      {_fmt(d.get('rent_p25_czk'))} / {_fmt(d.get('rent_p75_czk'))} CZK")
    print(f"confidence:     {d.get('confidence') or '—'}")
    print(f"# comparables:  {len(d.get('comparables_used') or [])}")
    warnings = d.get("warnings") or []
    if warnings:
        print(f"# warnings:     {len(warnings)}")
        for w in warnings[:3]:
            print(f"  - {w}")

    trace = recorder.to_dict("smoke")
    print(f"\nTrace steps ({len(trace['steps'])} total, first 5 shown):")
    for step in trace["steps"][:5]:
        kind = step.get("kind")
        if kind == "reasoning":
            text = (step.get("output_summary") or {}).get("text", "")
            text = text[:80] + "…" if len(text) > 80 else text
            queued = (step.get("output_summary") or {}).get("tool_calls_queued")
            print(f"  [{step['n']}] reasoning {queued}: {text!r}")
        elif kind == "tool_call":
            print(f"  [{step['n']}] tool {step.get('tool')!r} ({step.get('duration_ms')}ms): {step.get('output_summary')}")
        else:
            print(f"  [{step['n']}] {kind}: {step.get('output_summary')}")


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{int(v):,}".replace(",", " ")
    return str(v)


class _DummySrealityClient:
    """The agent's verify_listing_freshness tool needs an SrealityClient.

    In the happy-path smoke flow the agent doesn't typically call it
    (and if it does, the freshness toolkit will short-circuit when the
    listing's last_seen_at is recent). If the agent actually invokes
    freshness fetch, the dummy will raise — which surfaces in the
    trace as a tool error rather than a crash. That's intentional:
    smoke shouldn't burn a real sreality detail fetch.
    """
    def fetch_detail_html(self, sreality_id: int) -> str:
        raise RuntimeError(
            "smoke_agent.py uses a dummy SrealityClient; "
            "live freshness fetches are disabled."
        )


if __name__ == "__main__":
    sys.exit(main())
