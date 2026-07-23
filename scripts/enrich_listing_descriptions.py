"""Enrich description-only listings (bazos) with typed attributes via a cheap LLM.

For each active listing of --source that has a description and whose latest
snapshot isn't yet enriched, extract floor / amenities / condition /
building_type / energy from the free text and fill the currently-NULL listings
columns. Resumable (enriched snapshots drop out of the next selection), bounded
by --limit and --max-cost-usd. Exits 1 after a run of consecutive per-listing
errors (provider outage) so the workflow goes red. Typically run via
.github/workflows/enrich_bazos.yml.

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
        # Gate 2 of the listing-identity refactor makes new non-sreality rows
        # insert sreality_id = NULL. This whole enrichment lane is sreality_id-
        # keyed end-to-end — the returned handle feeds enrich_listing_description /
        # build_enrich_request (toolkit.bazos_enrichment, all `WHERE sreality_id=%s`)
        # and lands in listing_description_enrichment_batch_requests.sreality_id
        # (NOT NULL, no listing_id column) — so a NULL-sreality row cannot be
        # processed here. Without this guard `int(None)` (below) crashes the run, and
        # because the NOT-EXISTS anti-join is never true for a NULL and ORDER BY
        # first_seen_at DESC floats the newest (NULL) row to the top, every
        # subsequent run re-crashes on the same row (the lane dies permanently).
        # Migrating the lane onto listings.id (toolkit + a batch-table migration)
        # is out of this fix's scope; until then NULL-sreality rows are skipped.
        "  AND l.sreality_id IS NOT NULL "
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


def _count_null_identity_skipped(conn: Any, *, source: str) -> int:
    """How many active, description-bearing `source` listings _select_pending's
    `sreality_id IS NOT NULL` guard is currently excluding.

    0 today (Gate 2 hasn't flipped: every existing row still carries a
    sreality_id, real or negative-synthetic). Once it flips, new non-sreality
    rows land with sreality_id NULL and this lane — sreality_id-keyed
    end-to-end, see the guard's comment in _select_pending — can never enrich
    them; this makes that silent, permanent skip visible in the run log
    instead of only being discoverable by reading the SQL.
    """
    sql = (
        "SELECT count(*) FROM listings l "
        "WHERE l.is_active = true "
        "  AND l.source = %s "
        "  AND l.sreality_id IS NULL "
        "  AND l.description IS NOT NULL "
        "  AND length(btrim(l.description)) > 0"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (source,))
        row = cur.fetchone()
        return int(row[0]) if row else 0


# A provider outage (dead key, exhausted credit, sustained 5xx) fails EVERY
# call; without an abort the loop burns the whole wall-clock budget logging
# per-listing errors. Skips/successes reset the streak.
_MAX_CONSECUTIVE_ERRORS = 5


def _enrich_loop(
    conn: Any,
    llm_client: Any,
    ids: list[int],
    *,
    model: str,
    max_cost_usd: float,
    max_seconds: int,
    enrich: Any,
) -> tuple[dict[str, Any], bool]:
    """Run the per-listing enrichment loop. Returns (stats, aborted).

    aborted=True means _MAX_CONSECUTIVE_ERRORS in a row — a provider outage,
    not per-listing flakiness — and the caller should exit non-zero so the
    workflow run goes red instead of silently green-but-useless.
    """
    start = time.monotonic()
    stats: dict[str, Any] = {"ok": 0, "filled": 0, "skipped": 0, "errors": 0, "spent": 0.0}
    consecutive_errors = 0
    for i, sid in enumerate(ids, 1):
        if stats["spent"] >= max_cost_usd:
            LOG.info(
                "ENRICH cost cap reached spent=%.2f cap=%.2f at %d/%d",
                stats["spent"], max_cost_usd, i - 1, len(ids),
            )
            break
        if max_seconds > 0 and time.monotonic() - start >= max_seconds:
            LOG.info(
                "ENRICH time budget %ds reached at %d/%d; finalizing cleanly",
                max_seconds, i - 1, len(ids),
            )
            break
        try:
            res = enrich(conn, llm_client, sid, model=model)
        except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
            stats["errors"] += 1
            consecutive_errors += 1
            LOG.warning("ENRICH id=%s error=%s", sid, exc)
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                LOG.error(
                    "ENRICH aborting: %d consecutive errors (provider outage?) at %d/%d",
                    consecutive_errors, i, len(ids),
                )
                return stats, True
            continue
        consecutive_errors = 0
        if res.get("status") == "ok":
            stats["ok"] += 1
            stats["spent"] += float(res.get("cost_usd") or 0.0)
            stats["filled"] += len(res.get("filled") or [])
        else:
            stats["skipped"] += 1
        if i % 50 == 0:
            LOG.info(
                "ENRICH progress=%d/%d ok=%d filled=%d skipped=%d errors=%d spent=%.2f",
                i, len(ids), stats["ok"], stats["filled"], stats["skipped"],
                stats["errors"], stats["spent"],
            )
    return stats, False


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
    from api.providers.openai import OpenAIProvider
    from scraper import db
    from toolkit.bazos_enrichment import enrich_listing_description, resolve_enrichment_model

    with db.connect() as conn:
        # --model wins; else the operator's enrichment_model setting (Haiku default).
        # LLMClient.call derives the backend from the id, so gpt-5-mini routes to OpenAI.
        model = args.model or resolve_enrichment_model(conn)
        LOG.info(
            "ENRICH config source=%s limit=%d max_cost=%.2f max_seconds=%d model=%s dry_run=%s",
            args.source, args.limit, args.max_cost_usd, args.max_seconds, model, args.dry_run,
        )
        ids = _select_pending(
            conn, source=args.source, model=model,
            max_age_days=args.max_age_days, limit=args.limit,
        )
        LOG.info("ENRICH pending=%d", len(ids))
        null_identity_skipped = _count_null_identity_skipped(conn, source=args.source)
        if null_identity_skipped:
            LOG.warning(
                "ENRICH %d active %s listings permanently skipped (NULL sreality_id, "
                "Gate-2 flip active) -- this lane needs a listings.id migration to reach them",
                null_identity_skipped, args.source,
            )
        if args.dry_run:
            LOG.info("ENRICH dry-run: would enrich %d listings", len(ids))
            return 0

        llm_client = LLMClient(conn, providers={
            "anthropic": AnthropicProvider(), "openai": OpenAIProvider(),
        })
        stats, aborted = _enrich_loop(
            conn, llm_client, ids,
            model=model, max_cost_usd=args.max_cost_usd,
            max_seconds=args.max_seconds, enrich=enrich_listing_description,
        )
        LOG.info(
            "ENRICH done ok=%d filled_fields=%d skipped=%d errors=%d spent_usd=%.2f aborted=%s",
            stats["ok"], stats["filled"], stats["skipped"], stats["errors"],
            stats["spent"], aborted,
        )
    return 1 if aborted else 0


if __name__ == "__main__":
    sys.exit(main())
