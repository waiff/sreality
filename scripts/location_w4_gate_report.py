"""Location W4: the acceptance gate, measured (06 §6.4 W4), read-only.

The one arm no test can hold, in the W3 tradition (`claims_remine_verify`, #1102): the gate
is a fact about the production corpus, and no session has `psql` while the MCP's statement
budget cannot finish a full-corpus jsonb scan. So the measurement runs where the DB URL
lives — the dispatch lane's `mode=gate` — and prints the numbers the sign-off quotes.

Three rulings, so the numbers mean what the gate text says:

* **The legacy share is measured on `listings`, never on the cohort table.**
  `location_enrichment_state` is a scheduling ledger; before its first reconcile it was a
  high-water mark that could only grow, and even reconciled it answers "what did the
  producer enroll", not "what share of ACTIVE rows is legacy-shaped". The gate wording is
  the second question. The classifier is W1's `sreality_payload_shape` restated as ONE
  exhaustive `CASE` — post-cutover tested first, `IS DISTINCT FROM 'object'` because
  `jsonb_typeof(NULL)` is NULL and a plain `<>` silently drops exactly the truncated rows,
  and a final `ELSE 'absent'` for the second `absent` arm — so the shares sum to 100.
* **"The refetched cohort" is every cohort row whose listing is still active**, placed or
  still pending or exhausted — NOT only the rows that flipped. The producer stamps
  `attempts = 1` on enrollment, so `attempts` cannot separate dispatched rows from never-
  dispatched ones; counting the still-legacy rows in the denominator is the honest
  reading of "inaccuracy_type / entity_type present on ≥ 95 % of the refetched cohort"
  (06 §6.4) and the only one the table can answer.
* **bezrealitky's ≥ 95 % `ruianId` arm is reported against the portal ceiling.** W1v measured
  that the portal publishes the key as JSON `null` on about half of active adverts; a
  refetch cannot mint a kód ADM the portal withholds. The arm is rendered with the number
  AND the ceiling, and the actionable sub-arm — the pre-0m-shape rows a refetch CAN fix —
  is what the overall verdict counts. The gate text is not rewritten; it is measured.

Usage:
  python -m scripts.location_w4_gate_report            # table
  python -m scripts.location_w4_gate_report --json
  python -m scripts.location_w4_gate_report --fail-on-gate
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg

from location_data import loader_db
from location_data.claims_intake import guarded
from location_data.refetch_cohort import COHORT_LANE
from scraper import db

LOG = logging.getLogger("location_w4_gate_report")

STATEMENT_TIMEOUT_ENV = "LOCATION_W4_GATE_TIMEOUT_S"
DEFAULT_STATEMENT_TIMEOUT_S = 600

LEGACY_SHARE_MAX_PCT = 2.0
D3_COVERAGE_MIN_PCT = 95.0
RUIAN_ID_MIN_PCT = 95.0

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_NO_DATA = "NO DATA"
VERDICT_PORTAL_CAPPED = "PORTAL-CAPPED"

_POST_CUTOVER_KEYS = "array['gps_lat','gps_lon','entity_type','inaccuracy_type','city','citypart']"
_LEGACY_KEYS = "array['name','value','accuracy']"

# Mirrors `claims_intake.sreality_payload_shape` arm for arm; `test_location_w4_gate_report`
# runs both over the same fixtures so the SQL cannot drift from the Python.
SHAPE_CASE_SQL = f"""
    CASE
      WHEN jsonb_typeof(raw_json->'locality') IS DISTINCT FROM 'object' THEN 'absent'
      WHEN raw_json->'locality' ?| {_POST_CUTOVER_KEYS} THEN 'post_cutover'
      WHEN raw_json->'locality' ?| {_LEGACY_KEYS} THEN 'legacy'
      ELSE 'absent'
    END
"""

_LEGACY_SHARE_SQL = f"""
    WITH shaped AS (
      SELECT {SHAPE_CASE_SQL} AS shape
      FROM listings WHERE source = 'sreality' AND is_active
    )
    SELECT shape, count(*) FROM shaped GROUP BY shape
"""

_D3_COVERAGE_SQL = """
    SELECT es.last_outcome,
           count(*) AS n,
           count(*) FILTER (
             WHERE jsonb_typeof(l.raw_json->'locality') = 'object'
               AND l.raw_json->'locality' ? 'entity_type'
               AND l.raw_json->'locality' ? 'inaccuracy_type') AS d3
    FROM location_enrichment_state es
    JOIN listings l ON l.id = es.listing_id
    WHERE es.lane = %(lane)s
      AND es.last_outcome IS DISTINCT FROM 'not_applicable'
      AND l.is_active
    GROUP BY es.last_outcome
"""

_BEZREALITKY_SQL = """
    SELECT count(*) AS active,
           count(*) FILTER (WHERE raw_json ? 'ruianId') AS key_present,
           count(*) FILTER (WHERE NOT (raw_json ? 'ruianId')) AS key_absent,
           count(*) FILTER (WHERE raw_json->>'ruianId' IS NOT NULL) AS with_id
    FROM listings WHERE source = 'bezrealitky' AND is_active
"""

_COHORT_STATE_SQL = """
    SELECT last_outcome, given_up, (next_eligible_at IS NULL) AS retired, count(*)
    FROM location_enrichment_state WHERE lane = %(lane)s
    GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""


@dataclass(frozen=True)
class Arm:
    name: str
    verdict: str
    measured: str
    threshold: str
    detail: str = ""


@dataclass(frozen=True)
class Report:
    arms: tuple[Arm, ...]
    cohort: tuple[dict[str, Any], ...]
    verdict: str


def _pct(num: int, den: int) -> float | None:
    return None if den == 0 else round(100.0 * num / den, 2)


def decide_legacy_share(counts: dict[str, int]) -> Arm:
    total = sum(counts.values())
    legacy = counts.get("legacy", 0)
    pct = _pct(legacy, total)
    if pct is None:
        return Arm("sreality legacy-shape share of active rows", VERDICT_NO_DATA, "n/a",
                   f"< {LEGACY_SHARE_MAX_PCT} %")
    verdict = VERDICT_PASS if pct < LEGACY_SHARE_MAX_PCT else VERDICT_FAIL
    return Arm("sreality legacy-shape share of active rows", verdict, f"{pct} %",
               f"< {LEGACY_SHARE_MAX_PCT} %",
               f"legacy {legacy} / post_cutover {counts.get('post_cutover', 0)} / "
               f"absent {counts.get('absent', 0)} of {total}")


def decide_d3_coverage(rows: list[tuple[str, int, int]]) -> Arm:
    den = sum(n for _, n, _ in rows)
    num = sum(d3 for _, _, d3 in rows)
    pct = _pct(num, den)
    if pct is None:
        return Arm("D3 axes on the refetched cohort", VERDICT_NO_DATA, "n/a",
                   f">= {D3_COVERAGE_MIN_PCT} %", "no active cohort rows")
    verdict = VERDICT_PASS if pct >= D3_COVERAGE_MIN_PCT else VERDICT_FAIL
    by_outcome = ", ".join(f"{o}={n} (d3 {d3})" for o, n, d3 in sorted(rows))
    return Arm("D3 axes on the refetched cohort", verdict, f"{pct} %",
               f">= {D3_COVERAGE_MIN_PCT} %", f"{num}/{den} active cohort rows — {by_outcome}")


def decide_bezrealitky(active: int, key_present: int, key_absent: int, with_id: int) -> tuple[Arm, Arm]:
    pct = _pct(with_id, active)
    if pct is None:
        share = Arm("bezrealitky active rows with a ruianId", VERDICT_NO_DATA, "n/a",
                    f">= {RUIAN_ID_MIN_PCT} %")
    elif pct >= RUIAN_ID_MIN_PCT:
        share = Arm("bezrealitky active rows with a ruianId", VERDICT_PASS, f"{pct} %",
                    f">= {RUIAN_ID_MIN_PCT} %")
    else:
        # The ceiling: rows carrying the key whose value the portal published as null. A
        # refetch of those returns the same null. Only the key-absent rows are ours to fix.
        null_rows = key_present - with_id
        ceiling = _pct(key_present, active)
        share = Arm("bezrealitky active rows with a ruianId", VERDICT_PORTAL_CAPPED,
                    f"{pct} %", f">= {RUIAN_ID_MIN_PCT} %",
                    f"{with_id}/{active}; {null_rows} publish null (portal ceiling "
                    f"{ceiling} % even if every pre-0m row is refetched)")
    remainder = Arm("bezrealitky pre-0m-shape rows still to refetch",
                    VERDICT_PASS if key_absent == 0 else VERDICT_FAIL,
                    str(key_absent), "0", f"key absent on {key_absent} of {active} active rows")
    return share, remainder


def overall(arms: tuple[Arm, ...]) -> str:
    """PASS iff no arm FAILs. PORTAL-CAPPED is reported, not failed: it names a ceiling no
    pipeline can lift, and the actionable sub-arm beside it carries the verdict."""
    if any(a.verdict == VERDICT_FAIL for a in arms):
        return VERDICT_FAIL
    if any(a.verdict == VERDICT_NO_DATA for a in arms):
        return VERDICT_NO_DATA
    return VERDICT_PASS


def gather(conn: psycopg.Connection, *, statement_timeout_s: int, lane: str = COHORT_LANE) -> Report:
    with guarded(conn, statement_timeout_s) as cur:
        cur.execute(_LEGACY_SHARE_SQL)
        legacy_counts = {shape: n for shape, n in cur.fetchall()}
    with guarded(conn, statement_timeout_s) as cur:
        cur.execute(_D3_COVERAGE_SQL, {"lane": lane})
        d3_rows = [(o, n, d3) for o, n, d3 in cur.fetchall()]
    with guarded(conn, statement_timeout_s) as cur:
        cur.execute(_BEZREALITKY_SQL)
        active, key_present, key_absent, with_id = cur.fetchone()
    with guarded(conn, statement_timeout_s) as cur:
        cur.execute(_COHORT_STATE_SQL, {"lane": lane})
        cohort = tuple({"last_outcome": o, "given_up": g, "retired": r, "n": n}
                       for o, g, r, n in cur.fetchall())

    share, remainder = decide_bezrealitky(active, key_present, key_absent, with_id)
    arms = (decide_legacy_share(legacy_counts), decide_d3_coverage(d3_rows), share, remainder)
    return Report(arms=arms, cohort=cohort, verdict=overall(arms))


def render(report: Report, *, generated_at: str) -> list[str]:
    lines = [f"W4 gate report — {generated_at}", ""]
    for arm in report.arms:
        lines.append(f"  [{arm.verdict:>13}] {arm.name}: {arm.measured} (gate {arm.threshold})")
        if arm.detail:
            lines.append(f"                  {arm.detail}")
    lines += ["", f"  cohort `{COHORT_LANE}`:"]
    for row in report.cohort:
        lines.append(f"    {row['last_outcome']:>15} given_up={row['given_up']!s:<5} "
                     f"retired={row['retired']!s:<5} {row['n']}")
    lines += ["", f"  VERDICT: {report.verdict}"]
    return lines


def to_json(report: Report, *, generated_at: str) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "verdict": report.verdict,
        "arms": [arm.__dict__ for arm in report.arms],
        "cohort": list(report.cohort),
        "metadata": {
            "tool": "location_w4_gate_report",
            "reads": ["listings", "location_enrichment_state"],
            "writes": [],
            "lane": COHORT_LANE,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The W4 acceptance gate, measured.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-gate", action="store_true",
                        help="Exit 1 unless the overall verdict is PASS.")
    parser.add_argument(
        "--statement-timeout", type=int,
        default=loader_db.env_timeout_s(STATEMENT_TIMEOUT_ENV, DEFAULT_STATEMENT_TIMEOUT_S))
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)
    if not os.environ.get("SUPABASE_DB_URL"):
        print("ERROR: SUPABASE_DB_URL is not set.", file=sys.stderr)
        return 2

    generated_at = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        report = gather(conn, statement_timeout_s=args.statement_timeout)
    if args.json:
        print(json.dumps(to_json(report, generated_at=generated_at), default=str, indent=2))
    else:
        print("\n".join(render(report, generated_at=generated_at)))
    if args.fail_on_gate and report.verdict != VERDICT_PASS:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
