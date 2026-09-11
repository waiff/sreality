"""Orchestrator for the bezrealitky.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.bezrealitky_main`. Bezrealitky is a `Portal`
(BezrealitkyPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages bezrealitky's GraphQL `listAdverts` and enqueues
new/price-changed ids into the shared `listing_detail_queue` (source='bezrealitky',
migration 108), then a detail-drain that fetches `advert(id)`, parses it to a
ScrapedListing, and ingests via `db.ingest_scraped_listing` (Tier-0 idempotency +
Tier-1 matching). No bespoke pipeline — only the per-portal fetcher
(BezrealitkyClient) + parser (bezrealitky_parser) + config differ from sreality.

Unlike bazos (a partial-walk HTML crawler), bezrealitky's GraphQL has no
deep-pagination cap, so a per-category walk can reach the portal's own end of
list: `supports_complete_walk` (config-driven) lets the runner nominate the rows
such a walk did not see for a page check, and the fetch decides (architectural
rule #3), source-scoped so it only ever touches bezrealitky rows (rule #15). Because
the detail JSON carries offerType/estateType, the drain derives each listing's
category from the response — so bezrealitky walks MANY categories from one config
without the queue-encodes-category limitation that constrains bazos.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from math import ceil
from typing import Any

from scraper import db, portal_runner
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
    default_config,
    deadline_reached,
    load_portal_config,
    classify_index_sighting,
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


class BezrealitkyPortal:
    """Bezrealitky as a Portal: the seams the generic runner needs, wrapping the
    bezrealitky GraphQL client + parser. Operational scope (categories,
    complete-walk capability) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 1.0  # baked floor; the instance reads it from config.limits

    def __init__(self, config: PortalConfig, *, max_pages: int | None = None) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self.index_rate = config.limits.index_rate
        self.shared_rate_limiter = config.limits.shared_rate_limiter
        self._price_change_min_pct = config.limits.price_change_min_pct

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

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        # Single-row ingest (ingest_scraped_listing), not batched prepared
        # writes, so the transaction pooler is fine — no session pooler needed.
        return db.connect()

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
        existing = (
            db.index_summary_native(conn, SOURCE, native_ids)
            if conn is not None else {}
        )
        new_ids = [n for n in native_ids if n not in existing]
        changed: list[str] = []
        unchanged_pks: list[int] = []
        for nid in native_ids:
            prev = existing.get(nid)
            if prev is None:
                continue
            if classify_index_sighting(
                prev, price_map.get(nid), self._price_change_min_pct,
            ) == "unchanged":
                unchanged_pks.append(prev["id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings_by_id(conn, unchanged_pks)

        entries = (
            [(n, None, price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, None, price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=bezrealitky new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
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
        return (
            seen, {"found_new": len(new_ids), "enqueued": enqueued},
            declared_total, pages, reached_end,
        )

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> BezrealitkyClient:
        return BezrealitkyClient(limiter=limiter)

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
        counts = {"new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}
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
            pk, result = db.ingest_scraped_listing(
                conn, listing, discovery_seq=it.discovery_seq,
                discovered_at=it.discovered_at)
            image_urls = listing.raw.get("image_urls") or []
            inserted = db.record_media(conn, pk, image_urls)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
        # Complete-walk portal: a gone detail flips that one listing inactive
        # immediately (mirrors sreality), then the runner dequeues it. Keyed on the
        # native id directly (not a sreality_id round-trip): post-Gate-2 the row's
        # sreality_id is NULL, so the legacy mark_listing_inactive would no-op.
        db.mark_listing_inactive_native(conn, SOURCE, native_id)

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'bezrealitky' AND claimed_at IS NULL AND given_up = false"
            )
            return int(cur.fetchone()[0])


def _load_config(dry_run: bool) -> PortalConfig:
    if dry_run:
        return default_config(SOURCE)
    try:
        with db.connect() as conn:
            return load_portal_config(conn, SOURCE)
    except Exception as exc:
        LOG.warning("load_portal_config failed: %s; using baked-in default", exc)
        return default_config(SOURCE)




def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    portal = BezrealitkyPortal(config, max_pages=args.max_pages)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # Newest-first delta probe (Wave C-2): diff + enqueue off the first index
    # page(s) only. No mark_inactive, no drain, no scrape_runs row.
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


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


if __name__ == "__main__":
    raise SystemExit(main())
