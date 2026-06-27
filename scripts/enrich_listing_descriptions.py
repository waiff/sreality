"""Enrich description-only listings (bazos) with typed attributes via a cheap LLM.

For each active listing of --source that has a description and whose latest
snapshot isn't yet enriched, extract floor / amenities / condition /
building_type / energy from the free text and fill the currently-NULL listings
columns. Resumable (enriched snapshots drop out of the next selection), bounded
by --limit and --max-cost-usd. Typically run via .github/workflows/enrich_bazos.yml.

    python -m scripts.enrich_listing_descriptions \\
        --source bazos --limit 500 --max-cost-usd 10
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Any

LOG = logging.getLogger("enrich_listing_descriptions")


def _select_pending(
    conn: Any, *, source: str, model: str, max_age_days: int, limit: int
) -> list[int]:
    """Active listings of `source` with a description whose latest snapshot isn't enriched BY `model`.

    Source-scoped and freshest-first off the existing (source, first_seen_at)
    index; the latest-snapshot check is a per-listing correlated subquery, NOT a
    global `MAX(id) ... GROUP BY` over the whole ~1M-row listing_snapshots table.
    The old global-CTE form aggregated every listing's history before filtering to
    one source and timed out (21s+); this form lets the planner walk only this
    source's rows and short-circuit at the LIMIT (~0.7s). Re-enriches a listing
    whose content changed since its last enrichment (its latest snapshot id moved)
    OR whose latest snapshot was only ever enriched by a DIFFERENT model — so
    upgrading the model re-attempts every listing (the cache is model-keyed,
    migration 249), instead of silently reusing an older model's misses.
    """
    freshness = " AND l.last_seen_at > now() - %s::interval" if max_age_days > 0 else ""
    sql = (
        "SELECT l.sreality_id "
        "FROM listings l "
        "WHERE l.is_active = true "
        "  AND l.source = %s "
        "  AND l.description IS NOT NULL "
        "  AND length(btrim(l.description)) > 0 "
        + freshness +
        "  AND NOT EXISTS ( "
        "    SELECT 1 FROM listing_description_enrichments e "
        "    WHERE e.sreality_id = l.sreality_id "
        "      AND e.model = %s "
        "      AND e.snapshot_id = ( "
        "        SELECT MAX(id) FROM listing_snapshots s "
        "        WHERE s.sreality_id = l.sreality_id "
        "      ) "
        "  ) "
        "ORDER BY l.first_seen_at DESC "
        "LIMIT %s"
    )
    params: tuple[Any, ...] = (
        (source, f"{max_age_days} days", model, limit) if max_age_days > 0
        else (source, model, limit)
    )
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [int(r[0]) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", default="bazos")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--max-cost-usd", type=float, default=10.0)
    ap.add_argument(
        "--max-seconds", type=int, default=0,
        help="Wall-clock budget; finalize cleanly when reached (0 = no budget). "
             "Keep below the workflow's timeout-minutes so a run is never cancelled "
             "mid-flight (the next cron tick resumes from the queue).",
    )
    ap.add_argument(
        "--max-age-days", type=int, default=0,
        help="0 = every active listing regardless of last_seen_at.",
    )
    ap.add_argument("--model", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
    )

    from api.llm_client import LLMClient
    from api.providers.anthropic import AnthropicProvider
    from scraper import db
    from toolkit.bazos_enrichment import DEFAULT_MODEL, enrich_listing_description

    model = args.model or DEFAULT_MODEL
    LOG.info(
        "ENRICH config source=%s limit=%d max_cost=%.2f max_seconds=%d model=%s dry_run=%s",
        args.source, args.limit, args.max_cost_usd, args.max_seconds, model, args.dry_run,
    )

    with db.connect() as conn:
        ids = _select_pending(
            conn, source=args.source, model=model,
            max_age_days=args.max_age_days, limit=args.limit,
        )
        LOG.info("ENRICH pending=%d", len(ids))
        if args.dry_run:
            LOG.info("ENRICH dry-run: would enrich %d listings", len(ids))
            return 0

        llm_client = LLMClient(conn, providers={"anthropic": AnthropicProvider()})
        start = time.monotonic()
        spent = 0.0
        ok = filled_total = skipped = errors = 0
        for i, sid in enumerate(ids, 1):
            if spent >= args.max_cost_usd:
                LOG.info(
                    "ENRICH cost cap reached spent=%.2f cap=%.2f at %d/%d",
                    spent, args.max_cost_usd, i - 1, len(ids),
                )
                break
            if args.max_seconds > 0 and time.monotonic() - start >= args.max_seconds:
                LOG.info(
                    "ENRICH time budget %ds reached at %d/%d; finalizing cleanly",
                    args.max_seconds, i - 1, len(ids),
                )
                break
            try:
                res = enrich_listing_description(conn, llm_client, sid, model=model)
            except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
                errors += 1
                LOG.warning("ENRICH id=%s error=%s", sid, exc)
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                continue
            if res.get("status") == "ok":
                ok += 1
                spent += float(res.get("cost_usd") or 0.0)
                filled_total += len(res.get("filled") or [])
            else:
                skipped += 1
            if i % 50 == 0:
                LOG.info(
                    "ENRICH progress=%d/%d ok=%d filled=%d skipped=%d errors=%d spent=%.2f",
                    i, len(ids), ok, filled_total, skipped, errors, spent,
                )
        LOG.info(
            "ENRICH done ok=%d filled_fields=%d skipped=%d errors=%d spent_usd=%.2f",
            ok, filled_total, skipped, errors, spent,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
