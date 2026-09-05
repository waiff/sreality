"""The coverage gate: decide from evidence whether a parked portal may delist again.

`supports_complete_walk` gates delisting (architectural rule #3). Two portals are
parked on it: ceskereality (migration 449) and idnes (453). Both were parked for
the same reason — the flag was a standing claim someone typed once, and the walks
stopped matching it — so un-parking by hand would put us straight back there. A
person looks once, flips the flag, and the flag goes on asserting something
nobody re-checks.

So this runs on a schedule and answers two questions from data, every time:

  COVERED  Did every slice of every category finish (outcome='exhausted') inside
           the freshness window? One hole and the answer is no. Fourteen of
           fifteen slices is not 93% coverage for delisting purposes — the hole
           is exactly what `mark_inactive` reads as "these listings are gone".

  STABLE   Has that held for N consecutive evaluations, with the delist-candidate
           count steady between them? One lucky run proves nothing, and a
           candidate count that swings means the walk is flaky rather than that
           the market moved.

Both pass → `supports_complete_walk` goes back to true and the row records it.
Either fails → the flag stays down and the row records why. Every evaluation is
written either way (`portal_coverage_gate`, migration 455), because a verdict
that only exists in an expiring Actions log is a verdict nobody receives.

WHY THIS IS SAFE TO RUN UNATTENDED. Not because the gate is certain to be right —
because a wrong verdict cannot execute. Un-parking only makes a sweep *eligible*;
the flip cap (migrations 451/452) still refuses any sweep over 10% of a category,
latches, and alarms. idnes's backlog is ~37% of its rows, so the very failure this
gate could cause is the one the layer underneath is built to catch. The gate needs
to be right-or-caught, not right.

Usage:
    python -m scripts.coverage_gate                # evaluate + act
    python -m scripts.coverage_gate --dry-run      # evaluate + report only
    python -m scripts.coverage_gate --source idnes
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any

from scraper import db
from scraper.portal_factory import canonical_category_count

LOG = logging.getLogger("coverage_gate")

# How recent a slice's walk must be to count toward "covered". Generous on
# purpose: the walk runs every 6h, so 30h tolerates a skipped cycle plus a slow
# one without resetting the streak, while still refusing to call a three-day-old
# slice evidence of anything.
FRESHNESS_HOURS = 30

# Consecutive covered evaluations required. Three, because one is luck and two is
# a coincidence; the gate runs after each walk cycle, so three is about a day.
REQUIRED_CONSECUTIVE = 3

# How much the delist-candidate count may move between consecutive evaluations
# and still read as "stable". Real churn moves it a few percent a day; a flaky
# walk moves it by a third.
CANDIDATE_DRIFT_TOLERANCE = 0.15


_COVERAGE_SQL = """
select category_main, category_type,
       count(*)                                        as slices,
       count(*) filter (
         where outcome = %(positive)s
           and walked_at > now() - make_interval(hours => %(hours)s)
       )                                               as fresh_ok
  from portal_index_slices
 where source = %(source)s
 group by 1, 2
"""

# What a sweep would flip right now: active rows of this source whose last
# sighting predates the coverage window. This is the same shape as the sweep's
# own predicate, so the number in the ledger is the number that would move.
_CANDIDATE_SQL = """
select count(*)
  from listings
 where source = %(source)s
   and is_active
   and (last_seen_at is null
        or last_seen_at < now() - make_interval(hours => %(hours)s))
"""

_HISTORY_SQL = """
select covered, candidates
  from portal_coverage_gate
 where source = %(source)s
 order by evaluated_at desc
 limit %(limit)s
"""


def _declared_categories(source: str, categories: list[dict[str, Any]]) -> int:
    """The gate's denominator: how many canonical categories this portal's config
    maps to.

    It is NOT len(categories), and getting that wrong parks a portal forever.
    ceskereality declares both `rodinne-domy` and `chaty-chalupy` and both
    canonicalise to `dum`, so its 12 config entries can only ever write 10
    slice-ledger rows — a gate demanding 12 could never be satisfied, and it was
    not: ceskereality sat at "8/12 categories" every cycle for a week with no
    path out.

    Falls back to the raw count when the mapping cannot be resolved, which is the
    STRICT direction: the raw count is always >= the canonical count, so a
    fallback keeps the flag DOWN and can never open a gate by accident.
    """
    resolved = canonical_category_count(source, categories)
    if resolved is None:
        LOG.warning(
            "GATE %s: could not resolve canonical categories; using the raw "
            "config count (%d) — strict, so this can only hold the gate shut",
            source, len(categories),
        )
        return len(categories)
    if resolved != len(categories):
        LOG.info("GATE %s: %d config entries map to %d canonical categories",
                 source, len(categories), resolved)
    return resolved


def _parked_sources(conn: Any, only: str | None) -> list[tuple[str, list[dict[str, Any]]]]:
    """Portals whose delisting is parked — the only ones this gate can open."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select source, categories
              from portals
             where is_enabled
               and supports_complete_walk = false
               and (%s::text is null or source = %s)
             order by source
            """,
            (only, only),
        )
        return [(r[0], list(r[1] or [])) for r in cur.fetchall()]


def _coverage(conn: Any, source: str) -> tuple[int, int, int, int]:
    """(categories_seen, categories_fully_covered, slices_ok, slices_total)."""
    with conn.cursor() as cur:
        cur.execute(_COVERAGE_SQL, {
            "source": source, "hours": FRESHNESS_HOURS,
            "positive": db.SLICE_OUTCOME_POSITIVE,
        })
        rows = cur.fetchall()
    cats_ok = sum(1 for _cm, _ct, slices, ok in rows if slices == ok and ok > 0)
    return len(rows), cats_ok, sum(r[3] for r in rows), sum(r[2] for r in rows)


def _candidates(conn: Any, source: str) -> int:
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {"source": source, "hours": FRESHNESS_HOURS})
        return int(cur.fetchone()[0])


def _stable(history: list[tuple[bool, int | None]], candidates: int) -> bool:
    """Did the candidate count hold steady across the covered run of history?

    A walk that reaches every slice but enumerates a different population each
    time is not covering the portal, it is sampling it — and the difference is
    invisible in a coverage percentage. This is the check that sees it.
    """
    prior = [c for covered, c in history if covered and c is not None]
    if not prior:
        return True   # nothing to compare against yet; the streak length gates that
    ref = max(prior[0], 1)
    return abs(candidates - prior[0]) / ref <= CANDIDATE_DRIFT_TOLERANCE


def evaluate(conn: Any, source: str, declared_categories: int, *, dry_run: bool) -> dict[str, Any]:
    cats_seen, cats_ok, slices_ok, slices_total = _coverage(conn, source)
    candidates = _candidates(conn, source)

    with conn.cursor() as cur:
        cur.execute(_HISTORY_SQL, {"source": source, "limit": REQUIRED_CONSECUTIVE})
        history = [(bool(r[0]), r[1]) for r in cur.fetchall()]

    # Every category the portal declares must be covered — not every category the
    # ledger happens to know about. A category that has never been walked has no
    # ledger rows at all, and counting only what the ledger holds would let a
    # portal pass by walking a subset well.
    covered = bool(
        declared_categories > 0
        and cats_seen >= declared_categories
        and cats_ok >= declared_categories
        and slices_total > 0
        and slices_ok == slices_total
    )
    streak = 1 if covered else 0
    if covered:
        for prev_covered, _c in history:
            if not prev_covered:
                break
            streak += 1

    stable = _stable(history, candidates) if covered else False
    verdict, note = "hold", ""
    if not covered and slices_total == 0:
        # No ledger rows at all is a different statement from "walked and came up
        # short", and reading them the same way makes an uninstrumented portal
        # look like a broken one. Only idnes writes the slice ledger today.
        note = ("no slice ledger for this portal — its walk does not record "
                "portal_index_slices yet, so coverage cannot be evidenced")
    elif not covered:
        note = (f"{cats_ok}/{declared_categories} categories fully covered "
                f"({slices_ok}/{slices_total} slices fresh+exhausted within "
                f"{FRESHNESS_HOURS}h)")
    elif streak < REQUIRED_CONSECUTIVE:
        note = f"covered, but only {streak}/{REQUIRED_CONSECUTIVE} consecutive evaluations"
    elif not stable:
        note = (f"covered {streak}x, but the delist candidate count moved more than "
                f"{CANDIDATE_DRIFT_TOLERANCE:.0%} (now {candidates})")
    else:
        verdict = "pass"
        note = (f"covered {streak}x consecutively, {candidates} delist candidates, "
                f"count stable")

    if verdict == "pass" and not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                "update portals set supports_complete_walk = true where source = %s",
                (source,),
            )
        verdict = "unparked"
        LOG.warning(
            "GATE OPENED source=%s — supports_complete_walk is true again after %d "
            "consecutive fully-covered evaluations. %d rows are now delist-eligible; "
            "the flip cap still refuses any sweep over its share of a category and "
            "records the refusal.", source, streak, candidates,
        )

    row = {
        "source": source, "covered": covered, "categories": declared_categories,
        "categories_ok": cats_ok, "slices_ok": slices_ok,
        "slices_total": slices_total, "candidates": candidates,
        "consecutive": streak, "verdict": verdict, "note": note,
    }
    if not dry_run:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into portal_coverage_gate
                    (source, covered, categories, categories_ok, slices_ok,
                     slices_total, candidates, consecutive, verdict, note)
                values (%(source)s, %(covered)s, %(categories)s, %(categories_ok)s,
                        %(slices_ok)s, %(slices_total)s, %(candidates)s,
                        %(consecutive)s, %(verdict)s, %(note)s)
                """,
                row,
            )
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default=None, help="evaluate one portal only")
    ap.add_argument("--dry-run", action="store_true",
                    help="evaluate and report; write nothing, un-park nothing")
    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not os.environ.get("SUPABASE_DB_URL"):
        LOG.error("SUPABASE_DB_URL is not set")
        return 2

    conn = db.connect()
    parked = _parked_sources(conn, args.source)
    if not parked:
        LOG.info("GATE no parked portals to evaluate — nothing to do")
        return 0

    for source, categories in parked:
        row = evaluate(conn, source, _declared_categories(source, categories),
                       dry_run=args.dry_run)
        LOG.info(
            "GATE source=%s verdict=%s covered=%s categories=%d/%d slices=%d/%d "
            "candidates=%d streak=%d — %s",
            source, row["verdict"], row["covered"], row["categories_ok"],
            row["categories"], row["slices_ok"], row["slices_total"],
            row["candidates"], row["consecutive"], row["note"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
