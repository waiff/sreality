"""Orchestrator for the bezrealitky.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.bezrealitky_main`. Bezrealitky is a `Portal`
(BezrealitkyPortal) driven by the generic `scraper.portal_runner`. Its own
`walk_category` pages bezrealitky's GraphQL `listAdverts` and hands its sightings
to `portal_runner.reconcile_sightings`, which touches and enqueues into the shared
`listing_detail_queue` (source='bezrealitky', migration 108); the detail-drain fetches `advert(id)`,
parses it to a ScrapedListing, and writes via `listing_write.write_listings`
(the one listing write; a first-seen row lands `property_id` NULL and the
straggler-attach births its singleton, rule #15).

bezrealitky's GraphQL has no deep-pagination cap, so a per-category walk can
reach the portal's own end of list. A walk that did (`walk_reached_end`, rule #3)
nominates the rows it did not see for a page check and the fetch decides,
source-scoped so it only ever touches bezrealitky rows (rule #15);
`supports_complete_walk` is posture only and gates nothing. Because the detail
JSON carries offerType/estateType, the drain derives each listing's category
from the response — so bezrealitky walks MANY categories from one config.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from math import ceil
from typing import Any

from scraper import db, listing_write, portal_runner
from scraper.bezrealitky_client import BezrealitkyClient, detail_payload_body
from scraper.bezrealitky_parser import (
    ESTATE_TYPE,
    OFFER_TYPE,
    SOURCE,
    parse_advert,
)
from scraper.portal import (
    PortalConfig,
    StopReason,
    deadline_reached,
    stop_is_portal_end,
    walk_reached_end,
)
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)

INDEX_PAGE_SIZE = 100

# An unconditional page ceiling for the offset loop. Its exits are all
# conditional on something the API says (an empty page, a positive declared
# count) or on a knob the scheduled lane may not pass (--max-seconds,
# --max-pages), so a degraded resolver that serves rows while declaring
# `totalCount: 0` would page forever until the job timeout SIGKILLs the run
# (no scrape_runs row, no drain). 4,000 pages is ~400,000 adverts, two orders
# of magnitude above bezrealitky's largest category, and it stops as `page_cap`
# — a stop of OURS, so hitting it can never nominate.
INDEX_PAGE_CEILING = 4_000

# No staleness rail any more (rule #3, 2026-09-07): a complete walk nominates
# the rows it did not see for a page check and the drain's fetch decides, so a
# single walk-miss costs one fetch, never a live listing.


def _end_of_list_confirmed(
    fetch: Callable[[int], tuple[list[dict[str, Any]], int]], *,
    offset: int, page_index: int, declared_total: int | None, page_total: int,
) -> bool:
    """Re-read the SAME page that came back empty; True only if the portal's
    list is really exhausted there (barren rule, rule #3).

    An items-less HTTP 200 is exactly what a soft-blocked GraphQL answer looks
    like here — `bezrealitky_client.search` turns a null `listAdverts` into
    ([], 0) with no error — so an empty page can never be the end on its own.
    It takes the same offset the loop was reading, so the confirmation cannot
    drift onto another page, and any failure to re-read returns False: a
    confirmation that cannot be obtained is not a confirmation.
    """
    try:
        adverts, total = fetch(offset)
    except Exception as exc:  # noqa: BLE001 - an unreadable re-read proves nothing
        LOG.warning("INDEX empty-page re-read failed offset=%d: %s", offset, exc)
        return False
    if adverts:
        return False
    if declared_total is None:
        # No page of this walk ever carried an item, so the count is the only
        # evidence there is — and both reads have to declare the scope empty.
        # (An empty category is a measured zero and a fact; a shell answer that
        # zeroes an over-declared total cannot pass, its first read carried the
        # real count.)
        return page_total == 0 and total == 0
    # At or past the position the declared total implies: the offset caught the
    # count, or this request is past the last page it implies AND the count
    # falls inside the window we just asked for — an over-declared total whose
    # list ran dry, which is a FINISHED walk, not a truncated one. A barren page
    # with a whole page of declared rows still unread is never the end: that is
    # what a mid-walk throttle looks like, including a throttled true last page.
    return offset >= declared_total or (
        page_index > ceil(declared_total / INDEX_PAGE_SIZE)
        and offset + INDEX_PAGE_SIZE > declared_total
    )


class BezrealitkyPortal(portal_runner.PortalDefaults):
    """Bezrealitky as a Portal: the seams the generic runner needs, wrapping the
    bezrealitky GraphQL client + parser. Operational scope (categories, rates)
    comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 1.0  # baked floor; the instance reads it from config.limits

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self.price_change_min_pct = config.limits.price_change_min_pct

    # --- index-walk seams ---
    def set_index_page_cap(self, pages: int | None) -> None:
        # Probe seam (portal_runner.run_index_probe): the GraphQL search already
        # orders TIMEORDER_DESC, so a page-capped walk IS the delta probe.
        self._max_pages = pages

    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        # Descriptors that group several estate types into one walk (KANCELAR +
        # NEBYTOVY_PROSTOR → "komercni", GARAZ + REKREACNI_OBJEKT → "ostatni")
        # carry the canonical `category_main` explicitly. Single-estate
        # descriptors derive it from the ESTATE_TYPE map.
        cm = category.get("category_main")
        if cm is None:
            et = category.get("estate_type")
            cm = ESTATE_TYPE.get(et) if isinstance(et, str) else None
        return (cm, OFFER_TYPE.get(category.get("offer_type")))

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
        deadline: float | None = None,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        offer = category["offer_type"]
        estate = category["estate_type"]
        # Per-descriptor scope knobs; default True (the listAdverts API default +
        # what bezrealitky.cz shows for CZ). A descriptor sets
        # include_imports:false only when that category's imports are aggregator
        # noise (PRONAJEM/REKREACNI is ~7000 vacation rentals).
        inc_imports = bool(category.get("include_imports", True))
        inc_short_term = bool(category.get("include_short_term", True))
        client = BezrealitkyClient(limiter=limiter)

        def fetch(off: int) -> tuple[list[dict[str, Any]], int]:
            return client.search(
                offer, estate, limit=INDEX_PAGE_SIZE, offset=off,
                include_imports=inc_imports, include_short_term=inc_short_term,
            )

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        offset = 0
        # Latched ONLY from a page that returned items AND declared a positive
        # count: a barren page reports totalCount 0, and letting it overwrite the
        # denominator would erase the only number the end-of-list rule has to
        # reason with (a page that hands us rows while declaring zero is
        # self-contradictory and is no denominator either).
        declared_total: int | None = None
        pages = 0
        stop: StopReason = "error"
        while True:
            adverts, total = fetch(offset)
            pages += 1
            LOG.info("INDEX offset=%d items=%d total=%d", offset, len(adverts), total)
            if adverts and total > 0 and (
                declared_total is None or total > declared_total
            ):
                # HIGH-WATER only: `offset >= declared_total` is a PORTAL end, so a
                # count that steps DOWN below the offset the walk already reached
                # would end it on a full page of items with the rest unwalked. A
                # number the walk has already passed is not evidence of an end.
                declared_total = total
            new_on_page = 0
            for adv in adverts:
                nid = str(adv["id"])
                if nid not in price_map:
                    native_ids.append(nid)
                    new_on_page += 1
                # Same clamps as the stored price so the unchanged-compare can't
                # see a value the write boundary would have nulled.
                price_map[nid] = db.sane_price_czk(adv.get("price"))
            offset += len(adverts)
            # Items-first: an items-less 200 shares its shape with a soft block,
            # so it gets its own arm and has to be corroborated before it may
            # count as the portal's end (rule #3).
            if not adverts:
                confirmed = _end_of_list_confirmed(
                    fetch, offset=offset, page_index=pages,
                    declared_total=declared_total, page_total=total,
                )
                stop = "empty_confirmed" if confirmed else "barren"
                if confirmed and declared_total is None and not native_ids:
                    declared_total = 0  # an empty scope, measured twice
                break
            # `offset` counts ROWS RETURNED, so an API that ignores or clamps the
            # offset parameter (a deep-offset clamp, a cache serving the head page)
            # would march it to the declared total while re-serving page 1 — and
            # exit `declared_total_reached` having seen one page. A page of items
            # that adds no new id means the list did not advance. On an offset API
            # that is OURS, not a terminator: unlike an HTML pager clamping an
            # out-of-range request onto its last page, nothing here says we are at
            # the end.
            if new_on_page == 0:
                stop = "pager_stalled"
                LOG.warning(
                    "INDEX offset did not advance offer=%s estate=%s offset=%d "
                    "items=%d collected=%d: the page carried no new advert",
                    offer, estate, offset, len(adverts), len(price_map),
                )
                break
            if declared_total is not None and offset >= declared_total:
                stop = "declared_total_reached"
                break
            # The natural end is tested FIRST: a walk that finished the portal's
            # last page exactly on the budget reached the end, it was not cut short.
            if deadline_reached(deadline):
                stop = "deadline"
                LOG.info(
                    "INDEX deadline reached offer=%s estate=%s page=%d offset=%d "
                    "collected=%d total=%s: stopping walk (incomplete)",
                    offer, estate, pages, offset, len(price_map), declared_total,
                )
                break
            if self._max_pages and pages >= self._max_pages:
                stop = "page_cap"
                break
            if pages >= INDEX_PAGE_CEILING:
                stop = "page_cap"
                LOG.warning(
                    "INDEX page ceiling reached offer=%s estate=%s pages=%d "
                    "collected=%d declared=%s; stopping (the walk nominates nothing)",
                    offer, estate, pages, len(price_map), declared_total,
                )
                break

        LOG.info(
            "INDEX walk end offer=%s estate=%s pages=%d collected=%d declared=%s stop=%s",
            offer, estate, pages, len(price_map), declared_total, stop,
        )

        seen = set(native_ids)
        counts = portal_runner.reconcile_sightings(
            conn, SOURCE, {n: (None, price_map.get(n)) for n in native_ids},
            min_change_pct=self.price_change_min_pct,
        )
        # STRUCTURAL, not numeric (rule #3, 2026-09-08): the walk may nominate
        # its unseen rows only if the PORTAL ended it. One flat offset walk per
        # descriptor means one stop reason for the whole category. A page cap is
        # our stop even when the loop ended naturally underneath it — it is an
        # operator restriction on this run, not a statement about coverage.
        # `declared_total` no longer gates anything; the runner records the
        # coverage verdict it implies into scrape_runs.by_category.
        portal_end = stop_is_portal_end(stop)
        reached_end = walk_reached_end(
            portal_end=portal_end, our_stop=bool(self._max_pages) or not portal_end,
        )
        return seen, counts, declared_total, pages, reached_end

    # --- detail-drain seams ---
    def fetch_detail(
        self, client: BezrealitkyClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        try:
            advert = client.get_detail(native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        try:
            listing = parse_advert(advert)
        except Exception as exc:
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        # The VERBATIM advert rides along for W2a-2's archive: listing.raw is the
        # advert plus the parser's derived image_urls, and an evidence substrate
        # that carries a field the portal never sent is not a substrate.
        return DrainItem(
            native_id=native_id, kind="ok",
            payload={"listing": listing, "advert": advert},
        )

    def write_details(self, conn: Any, items: list[DrainItem]) -> dict[str, int]:
        for it in items:
            listing = it.payload["listing"]
            # W2a-0 churn instrument: bezrealitky stages no body in
            # portal_raw_pages (the GraphQL advert goes straight into raw_json),
            # so the fetch is recorded here or the portal is absent from the
            # measurement. listing.raw IS the advert dict plus the parser's
            # derived image_urls, which is deterministic in the advert. The
            # serialisation is a thunk (nothing is dumped with the flag off) and
            # the observation token makes a replayed flush a no-op.
            # W2a-2: the same gap on the archive side, with the body 02 section
            # 2.3.2 P3 specifies for a graphql portal — the response data plus
            # the exact query text and its sha256. `advert` is absent only for an
            # item this portal's fetch_detail did not build (a hand-made drain
            # item in a test); falling back to listing.raw would archive the
            # parser's derived keys as if the portal had sent them.
            advert = it.payload.get("advert")
            if advert is not None:
                db.append_payload_if_enabled(
                    conn,
                    source=SOURCE,
                    source_id_native=listing.source_id_native,
                    page_kind="detail",
                    body=lambda: json.dumps(
                        detail_payload_body(advert), ensure_ascii=False,
                    ).encode("utf-8"),
                    content_type="application/json",
                )
        return listing_write.tally(listing_write.write_listings(conn, [
            listing_write.from_scraped(it.payload["listing"], discovered_at=it.discovered_at)
            for it in items
        ]))


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    portal_runner.configure_logging(args.verbose)

    config = portal_runner.load_config(SOURCE, args.dry_run)
    portal = BezrealitkyPortal(config, max_pages=args.max_pages)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Newest-first delta probe (Wave C-2): diff + enqueue off the first index
    # page(s) only. No nomination, no drain, no scrape_runs row.
    if args.probe:
        rc, _ = portal_runner.run_index_probe(
            portal, dry_run=args.dry_run, probe_pages=args.probe_pages)
        return rc

    # Index-walk (enqueue) then detail-drain (fetch + ingest), through the one
    # shared runner. Two scrape_runs rows ('index' + 'detail'), like sreality.
    rc = portal_runner.run_phase(
        portal, "index", portal_runner.run_index_walk, args.dry_run,
        max_seconds=args.max_seconds,
    )
    if rc == 0:
        rc = portal_runner.run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="bezrealitky.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; nominates "
             "nothing). Omit for a full walk to the portal's own end.",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = drain the queue)",
    )
    p.add_argument(
        "--workers", type=int, default=None,
        help="detail-fetch workers (default: per-portal config). advert(id) is "
             "~2-3s latency-bound, so concurrency (not the rate cap) sets "
             "throughput. Raise for a one-time backfill.",
    )
    p.add_argument(
        "--rate", type=float, default=None,
        help="detail-fetch requests/second ceiling (default: per-portal config)",
    )
    p.add_argument(
        "--probe", action="store_true",
        help="newest-first delta probe: diff + enqueue off the first "
             "--probe-pages index page(s) per category, then exit — never "
             "nominates unseen rows for a page check, no detail drain, no "
             "scrape_runs row",
    )
    p.add_argument(
        "--probe-pages", type=int, default=1,
        help="index pages per category for --probe (default 1; a page is "
             f"{INDEX_PAGE_SIZE} adverts)",
    )
    p.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help=(
            "Wall-clock budget for the run. The index walk stops cleanly on "
            "expiry and reports a stop of OURS, so nomination is "
            "suppressed (rule #3) rather than the job being killed by the CI "
            "timeout with nothing recorded; the drain uses it the same way."
        ),
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
