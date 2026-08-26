"""Framework entrypoint for the sreality scraper (Phase 4 portal framework).

Runnable as `python -m scraper.sreality_main`. Sreality is driven as a `Portal`
(SrealityPortal, defined in scraper.main next to the helpers it wraps) through
the one generic `scraper.portal_runner`, with the same CLI dialect as every
other portal (--index-only / --drain-only / --max-detail / --max-seconds /
--workers / --rate) and operational limits read from the `portals` registry
(CLI override > portals.operational_limits > scraper_limits_global > baked
default).

What stays sreality-specific lives behind the Portal seams, unchanged:
- the complete-walk semantics (all 12 category pairs incl. drazba/podil,
  per-district splitting above SPLIT_THRESHOLD with union-of-seen-sets + the
  national fallback pass, the INDEX_MIN_COMPLETENESS=1.0 gate, per-(cm,ct)
  mark_inactive) inside SrealityPortal.walk_category;
- the batched prepared writes (db.write_detail_batch on the session pooler)
  behind SrealityPortal.write_details — at sreality volume (~15k details/day)
  per-row ingest would forfeit the Phase-1 prepared-statement win;
- ListingGoneError -> immediate single-listing inactive flip + failure-row
  clear, and listing_fetch_failures bookkeeping, behind mark_gone /
  record_failure.

scraper.main keeps the legacy CLI (scrape.yml's instant-revert fallback) and
the image-download phase used by images.yml / images_fresh.yml — neither moves
here. Cadence split (rule #19): index_walk.yml runs `--index-only` every 15
min; detail_drain.yml runs `--drain-only` with a --max-seconds budget. Omitting
both flags runs both phases (dispatch-only combined fallback). Records an
'index' / a 'detail' scrape_runs row tagged source='sreality', with per-chunk
counter bumps + non-destructive finalize — exactly what Health liveness and the
per-portal stats read today.
"""

from __future__ import annotations

import argparse
import logging

from scraper import db, portal_runner
from scraper.main import SrealityPortal
from scraper.portal import PortalConfig, default_config, load_portal_config

LOG = logging.getLogger(__name__)
SOURCE = "sreality"


def _load_config(dry_run: bool) -> PortalConfig:
    if dry_run:
        return default_config(SOURCE)
    try:
        with db.connect() as conn:
            return load_portal_config(conn, SOURCE)
    except Exception as exc:  # noqa: BLE001 - registry hiccup must not break a scrape
        LOG.warning("load_portal_config failed: %s; using baked-in default", exc)
        return default_config(SOURCE)




def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    portal = SrealityPortal(index_rate=config.limits.index_rate)
    # The DB column is the source of truth for delisting (so it stays consistent
    # with the derived Health posture badge); the class default is the safe
    # fallback for the legacy main._run_full path that doesn't load config.
    portal.supports_complete_walk = config.supports_complete_walk
    portal.shared_rate_limiter = config.limits.shared_rate_limiter

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Cadence split (rule #19): --index-only walks + touches + marks inactive
    # under the completeness guard and enqueues into listing_detail_queue;
    # --drain-only claims a bounded slice and writes it batched. A combined run
    # (neither flag) is the dispatch-only fallback. Two scrape_runs rows.
    rc = 0
    if not args.drain_only:
        rc = portal_runner.run_phase(
            portal, "index", portal_runner.run_index_walk, args.dry_run,
            max_seconds=args.max_seconds,
        )
    if rc == 0 and not args.index_only:
        rc = portal_runner.run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
            max_seconds=args.max_seconds,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="sreality.cz scraper (portal framework)"
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (default: per-portal config)",
    )
    p.add_argument(
        "--workers", type=int, default=None,
        help="detail-fetch workers (default: per-portal config)",
    )
    p.add_argument(
        "--rate", type=float, default=None,
        help="detail-fetch requests/second ceiling across ALL workers "
             "(default: per-portal config). Auto-backs-off on HTTP 429/403.",
    )
    p.add_argument(
        "--max-seconds", type=float, default=None,
        help="wall-clock budget for a phase; it stops starting new work + "
             "finalizes cleanly before the job timeout (no 'stuck' run)",
    )
    p.add_argument(
        "--index-only", action="store_true",
        help="walk the index + enqueue + mark_inactive only (no detail drain)",
    )
    p.add_argument(
        "--drain-only", action="store_true",
        help="drain the detail queue only (no index walk)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
