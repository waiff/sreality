"""Orchestrator for the ceskereality.cz scraper — on the shared portal framework.

Runnable as `python -m scraper.ceskereality_main`. ceskereality is a `Portal`
(CeskerealityPortal) driven by the one generic `scraper.portal_runner`: an
index-walk that pages the HTML search results and enqueues new/price-changed ids
into the shared `listing_detail_queue` (source='ceskereality', migration 108),
then a detail-drain that fetches each listing page, parses it to a
`ScrapedListing`, and ingests via `db.ingest_scraped_listing` (Tier-0 idempotency
+ Tier-1 matching). No bespoke pipeline — only the per-portal fetcher
(CeskerealityClient) + parser (ceskereality_parser) + config differ from
sreality/idnes (the modularity rule in CLAUDE.md).

Like idnes, ceskereality's search pages carry a result total (the meta "Máme tady
N…") and have no deep-pagination cap (deep pages return real listings; the tail
is genuinely empty), so a per-category walk is provable-complete:
`supports_complete_walk` (config-driven) lets the runner mark delisted listings
inactive under the completeness guard (architectural rule #3), source-scoped so it
only ever touches ceskereality rows (rule #15). The detail URL carries the
category (`/{sale}/{cat}/…`), so the drain derives each listing's category from
its own URL — one config walks many categories. Coordinates come straight from the
page's `data-coord-lat`/`data-coord-lng`, so there is no geocoding step.
"""

from __future__ import annotations

import argparse
import logging
import re
import unicodedata
from typing import Any

from scraper import db, portal_runner
from scraper.ceskereality_client import (
    CeskerealityClient,
    detail_url,
    search_url,
)
from scraper.ceskereality_parser import (
    CATEGORY_MAIN,
    SALE_TYPE,
    category_from_url,
    extract_facet_slugs,
    index_price,
    parse_detail,
    parse_index,
)
from scraper.portal import PortalConfig, default_config, load_portal_config
from scraper.portal_base import ListingGoneError
from scraper.portal_runner import DrainItem
from scraper.rate_limit import RateLimiter

LOG = logging.getLogger(__name__)
SOURCE = "ceskereality"

# An index walk that collected at least this fraction of the page-reported total
# is treated as complete enough to drive mark_inactive; below it the walk likely
# truncated and flipping unseen listings inactive would falsely delist live ones.
# The framework standard (rule #3, matches idnes); the 24h min_unseen_hours rail
# in db.mark_inactive is the real safety against a tolerated walk-miss.
INDEX_MIN_COMPLETENESS = 0.995

# Anonymous search hard-caps at 12 pages (~240 results); ?strana=13 returns 404.
# So the walk NEVER requests page 13 — it slices each category by the COMPLETE okres
# (district) partition (admin_boundaries) × the page's disposition facets to keep
# every query under the cap, and marks a slice that still exceeds it as incomplete
# (suppressing mark_inactive).
_CAP_PAGES = 12
_PER_PAGE = 20

# Detail + search are always fetched on the canonical host; the okres slug is the
# locality filter (`/{sale}/{cat}/{okres}/`), so one host covers every district.
_WWW = "www.ceskereality.cz"

# The capital's admin name ("území Hlavního města Prahy") doesn't fold to
# ceskereality's slug for Prague, so it's mapped explicitly; every other okres folds
# its name -> slug directly (e.g. "Mladá Boleslav" -> mlada-boleslav, "Brno-město" ->
# brno-mesto, "Ústí nad Labem" -> usti-nad-labem).
_PRAHA_OKRES_ID = 9999
_PRAHA_SLUG = "praha-hlavni-mesto"


def _slugify(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.strip().lower())
    ascii_text = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def _walk_complete(collected: int, total: int | None) -> bool:
    if not total or total <= 0:
        return True
    return collected >= total * INDEX_MIN_COMPLETENESS


class CeskerealityPortal:
    """ceskereality.cz as a Portal: the seams the generic runner needs, wrapping
    the ceskereality client + parser. Operational scope (categories, complete-walk
    capability) comes from the `portals` registry config."""

    source = SOURCE
    index_rate = 0.7

    def __init__(
        self,
        config: PortalConfig,
        *,
        max_pages: int | None = None,
        regions: tuple[str, ...] | None = None,
    ) -> None:
        self.supports_complete_walk = config.supports_complete_walk
        self._categories = config.categories
        self._max_pages = max_pages
        self._okres_cache: list[str] | None = None
        # An okres/facet slug subset to walk (for an ad-hoc partial test); None =
        # the full okres split. When set, the walk is partial so mark_inactive is
        # suppressed.
        self._regions = regions
        self.index_rate = config.limits.index_rate
        # A (category_main, category_type) can be covered by MORE THAN ONE walk-
        # category — rodinne-domy AND chaty-chalupy both map to cm='dum'. mark_inactive
        # is scoped by (cm, ct, source), so a single complete sub-walk would flip the
        # SIBLING sub-walk's uncollected listings as falsely delisted. So accumulate
        # the seen ids + completeness across all contributors and flip ONCE, with the
        # union, only when EVERY contributor walked completely (rule #3).
        self._cmct_contributors: dict[tuple[str | None, str | None], int] = {}
        for c in self._categories:
            key = self.category_labels(c)
            self._cmct_contributors[key] = self._cmct_contributors.get(key, 0) + 1
        self._cmct_seen: dict[tuple[str | None, str | None], set[str]] = {}
        self._cmct_complete: dict[tuple[str | None, str | None], bool] = {}
        self._cmct_walked: dict[tuple[str | None, str | None], int] = {}
        self._cmct_flipped: set[tuple[str | None, str | None]] = set()

    # --- index-walk seams ---
    def categories(self) -> list[dict[str, Any]]:
        return list(self._categories)

    def category_labels(self, category: dict[str, Any]) -> tuple[str | None, str | None]:
        return (
            CATEGORY_MAIN.get(category.get("category")),
            SALE_TYPE.get(category.get("sale_type")),
        )

    def connect_index(self) -> Any:
        return db.connect()

    def connect_drain(self) -> Any:
        return db.connect()

    def _walk_slice(
        self, client: CeskerealityClient, host: str, sale_type: str, cat: str,
        sub_slug: str | None,
    ) -> tuple[list[tuple[str, str, int | None]], int, int | None, bool]:
        """Walk one region×facet slice, ≤12 pages — NEVER requesting page 13 (it
        404s). Returns (rows, pages_fetched, slice_total, complete); complete=False
        if the slice still exceeds the cap (we could only take its top ~240)."""
        rows: list[tuple[str, str, int | None]] = []
        total: int | None = None
        page = 1
        page_cap = min(_CAP_PAGES, self._max_pages or _CAP_PAGES)
        while page <= page_cap:
            url = search_url(
                sale_type, cat, host=host, sub_slug=sub_slug,
                page=page if page > 1 else None,
            )
            try:
                html, _ = client.fetch_search(url)
            except ListingGoneError:
                break                       # past the cap / empty slice -> end
            except Exception as exc:        # noqa: BLE001 - one slice must not kill the walk
                LOG.warning("SLICE error host=%s slug=%s page=%d: %s",
                            host, sub_slug, page, exc)
                break
            parsed = parse_index(html)
            if parsed.total is not None:
                total = parsed.total
            if not parsed.items:
                break
            for item in parsed.items:
                rows.append((
                    item.source_id_native,
                    detail_url(item.detail_path),
                    index_price(item.price_text),
                ))
            last_page = (total + _PER_PAGE - 1) // _PER_PAGE if total else None
            if parsed.next_offset is None:
                break
            if last_page is not None and page >= last_page:
                break
            page += 1
        capped = bool(total and total > _CAP_PAGES * _PER_PAGE and page >= _CAP_PAGES)
        return rows, page, total, (not capped and not self._max_pages)

    def _nationwide_total(self, client: CeskerealityClient, sale_type: str, cat: str) -> int | None:
        """The www result total — the portal-reported count for the RECONCILE +
        completeness gate (the per-region slices only report their own subset)."""
        try:
            html, _ = client.fetch_index(sale_type, cat, None)
            return parse_index(html).total
        except Exception:                   # noqa: BLE001
            return None

    def _okres_slugs(self, conn: Any) -> list[str]:
        """The COMPLETE okres (district) partition from admin_boundaries — the
        geographic axis of the cap-beating split. Every listing sits in exactly one
        okres, so walking all 77 covers the whole country, unlike the site's facet
        which only links its top ~9 districts per region."""
        if self._okres_cache is not None:
            return self._okres_cache
        slugs = [_PRAHA_SLUG]
        if conn is not None:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT name FROM admin_boundaries "
                        "WHERE level = 'okres' AND id <> %s",
                        (_PRAHA_OKRES_ID,),
                    )
                    slugs += [s for (name,) in cur.fetchall() if (s := _slugify(name))]
            except Exception as exc:        # noqa: BLE001
                LOG.warning("okres slug load failed: %s", exc)
        self._okres_cache = slugs
        return slugs

    def _sublocality_slugs(
        self, client: CeskerealityClient, sale_type: str, cat: str, okres_slug: str,
    ) -> list[str]:
        """The municipality / city-part facets an okres page links — `obec-{town}`
        (e.g. obec-slany) and `cast-{city}-{quarter}` (e.g. cast-praha-zizkov). Each
        is a single-facet `/{sale}/{cat}/{slug}/` sub-partition of the okres, so a
        dense okres that caps recurses into them. Category-agnostic (every listing
        sits in an obec), so it works for pozemky/komercni too — they have no
        disposition to split on."""
        try:
            html, _ = client.fetch_search(search_url(sale_type, cat, sub_slug=okres_slug))
            return [
                s for s in extract_facet_slugs(html, sale_type, cat)
                if s.startswith(("obec-", "cast-"))
            ]
        except Exception:                   # noqa: BLE001
            return []

    def _walk_okres(
        self, client: CeskerealityClient, sale_type: str, cat: str, okres_slug: str,
    ) -> tuple[list[tuple[str, str, int | None]], int, int | None, bool]:
        """Walk one okres; if it still caps (a dense district > 240), recurse into its
        obce / city-parts so every leaf stays under the 12-page cap. Complete iff the
        okres walk didn't cap OR the recursion collected ~all of the okres total."""
        rows, pages, total, complete = self._walk_slice(
            client, _WWW, sale_type, cat, okres_slug)
        if complete or self._max_pages:
            return rows, pages, total, complete
        sub = self._sublocality_slugs(client, sale_type, cat, okres_slug)
        if not sub:
            return rows, pages, total, False   # nothing to drill into -> incomplete
        collected = {nid for nid, _, _ in rows}
        all_rows = list(rows)
        for child in sub:
            crows, cpages, _ct, _cc = self._walk_slice(
                client, _WWW, sale_type, cat, child)
            pages += cpages
            for r in crows:
                if r[0] not in collected:
                    collected.add(r[0])
                    all_rows.append(r)
        # Complete iff the okres + its sub-localities reached the okres's own total
        # (a truncated obec facet leaves it short -> stays incomplete, suppressing
        # mark_inactive for the category, which is the conservative correct choice).
        return all_rows, pages, total, _walk_complete(len(collected), total)

    def walk_category(
        self, category: dict[str, Any], conn: Any, dry_run: bool, limiter: RateLimiter,
    ) -> tuple[set[str], dict[str, int], int | None, int, bool]:
        sale_type, cat = category["sale_type"], category["category"]
        client = CeskerealityClient(limiter=limiter)
        # Geographic axis = the COMPLETE okres partition (admin_boundaries), NOT the
        # site's truncated district facet (it only links its top ~9 districts per
        # region). Every CZ listing sits in exactly one okres, so the union covers the
        # whole country; a dense okres that still caps recurses into its obce/city-
        # parts (_walk_okres). `--region` (self._regions) restricts to a slug subset
        # for an ad-hoc partial test and, being partial, suppresses mark_inactive.
        scoped = self._regions
        okres_slugs = [
            s for s in self._okres_slugs(conn) if not scoped or s in scoped
        ]

        native_ids: list[str] = []
        price_map: dict[str, int | None] = {}
        ref_map: dict[str, str] = {}
        seen_ids: set[str] = set()
        pages = 0
        incomplete_okresy = 0

        def _absorb(rows: list[tuple[str, str, int | None]]) -> None:
            for nid, ref, price in rows:
                if nid not in seen_ids:
                    seen_ids.add(nid)
                    native_ids.append(nid)
                ref_map[nid] = ref
                price_map[nid] = price

        # The bare www page (top-240) — cheap insurance for any okres-less listing;
        # only on a full walk (noise under a scoped ad-hoc test).
        if not scoped:
            rows, slice_pages, _t, _c = self._walk_slice(
                client, _WWW, sale_type, cat, None)
            pages += slice_pages
            _absorb(rows)

        for okres in okres_slugs:
            rows, okres_pages, _t, okres_complete = self._walk_okres(
                client, sale_type, cat, okres)
            pages += okres_pages
            if not okres_complete:
                incomplete_okresy += 1
            _absorb(rows)

        total = self._nationwide_total(client, sale_type, cat)
        LOG.info(
            "SPLIT cm=%s ct=%s okresy=%d collected=%d total=%s "
            "incomplete_okresy=%d pages=%d",
            cat, sale_type, len(okres_slugs), len(seen_ids), total,
            incomplete_okresy, pages,
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
            if price_map.get(nid) is not None and prev["price_czk"] == price_map[nid]:
                unchanged_pks.append(prev["sreality_id"])
            else:
                changed.append(nid)

        if conn is not None and unchanged_pks:
            db.touch_listings(conn, unchanged_pks)

        entries = (
            [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_CHANGED) for n in changed]
            + [(n, ref_map[n], price_map.get(n), db.QUEUE_PRIORITY_NEW) for n in new_ids]
        )
        enqueued = (
            db.enqueue_detail(conn, SOURCE, entries)
            if conn is not None and entries else 0
        )
        LOG.info(
            "ENQUEUE source=ceskereality new=%d changed=%d unchanged=%d enqueued=%d",
            len(new_ids), len(changed), len(unchanged_pks), enqueued,
        )
        # mark_inactive is safe only on a FULL, uncapped walk: every okres walked
        # (not --region scoped), no okres left incomplete after its recursion, and we
        # collected ~all of the nationwide total. Any okres whose obce facet truncated
        # (recursion fell short of its total) leaves the walk incomplete, so we
        # suppress the sweep (rule #3).
        complete = (
            not self._max_pages and not self._regions and incomplete_okresy == 0
            and _walk_complete(len(seen), total)
        )
        # Accumulate this walk into its (cm, ct) bucket so mark_inactive can flip the
        # shared scope ONCE, with the union of every contributing sub-walk's seen ids,
        # and only when ALL are complete (the rodinne-domy/chaty-chalupy -> dum case).
        key = self.category_labels(category)
        self._cmct_seen.setdefault(key, set()).update(seen)
        self._cmct_complete[key] = self._cmct_complete.get(key, True) and complete
        self._cmct_walked[key] = self._cmct_walked.get(key, 0) + 1
        return seen, {"found_new": len(new_ids), "enqueued": enqueued}, total, pages, complete

    def mark_inactive(self, conn: Any, category: dict[str, Any], seen: set[str]) -> int:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return 0
        key = (cm, ct)
        # Wait until every walk-category contributing to this (cm, ct) has been walked,
        # then flip ONCE with the union of their seen ids — and only if ALL were
        # complete. A sibling sub-walk being incomplete (e.g. rodinne-domy's dense
        # okres capped) keeps the whole dum scope from flipping, so the complete
        # chaty-chalupy walk can't falsely delist uncollected rodinne-domy rows.
        if self._cmct_walked.get(key, 0) < self._cmct_contributors.get(key, 1):
            return 0
        if key in self._cmct_flipped or not self._cmct_complete.get(key, False):
            return 0
        self._cmct_flipped.add(key)
        union = self._cmct_seen.get(key) or set(seen)
        existing = db.index_summary_native(conn, SOURCE, list(union))
        pks = {v["sreality_id"] for v in existing.values()}
        return db.mark_inactive(conn, cm, ct, pks, source=SOURCE)

    def active_count(self, conn: Any, category: dict[str, Any]) -> int | None:
        cm, ct = self.category_labels(category)
        if cm is None or ct is None:
            return None
        return db.active_count(conn, cm, ct, source=SOURCE)

    # --- detail-drain seams ---
    def make_client(self, limiter: RateLimiter) -> CeskerealityClient:
        return CeskerealityClient(limiter=limiter)

    def fetch_detail(
        self, client: CeskerealityClient, native_id: str, detail_ref: str | None,
    ) -> DrainItem:
        url = detail_url(detail_ref or native_id)
        try:
            html, status = client.fetch_detail(detail_ref or native_id)
        except ListingGoneError:
            return DrainItem(native_id=native_id, kind="gone")
        except Exception as exc:  # noqa: BLE001 - one listing must not kill the run
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        cm, ct = category_from_url(url)
        try:
            listing = parse_detail(
                html, source_url=url, category_main=cm, category_type=ct,
            )
        except Exception as exc:  # noqa: BLE001
            return DrainItem(native_id=native_id, kind="error", error=str(exc))
        return DrainItem(
            native_id=native_id, kind="ok",
            payload={"listing": listing, "html": html, "status": status, "url": url},
        )

    def write_details(self, conn: Any, items: list[DrainItem]) -> dict[str, int]:
        counts = {"new": 0, "updated": 0, "unchanged": 0, "images_discovered": 0}
        for it in items:
            p = it.payload
            page_id = db.upsert_portal_raw_page(
                conn, source=SOURCE, source_id_native=it.native_id,
                source_url=p["url"], page_kind="detail",
                html=p["html"], http_status=p["status"],
            )
            pk, result = db.ingest_scraped_listing(conn, p["listing"])
            image_urls = p["listing"].raw.get("image_urls") or []
            inserted = db.record_media(conn, pk, image_urls)
            db.mark_portal_page_parsed(conn, page_id)
            if result in counts:
                counts[result] += 1
            counts["images_discovered"] += inserted
        return counts

    def mark_gone(self, conn: Any, native_id: str) -> None:
        existing = db.index_summary_native(conn, SOURCE, [native_id])
        row = existing.get(native_id)
        if row is not None:
            db.mark_listing_inactive(conn, row["sreality_id"])

    def record_failure(self, conn: Any, native_id: str, message: str) -> None:
        # The queue (fail_detail) tracks attempts/give-up; non-sreality sources
        # have no sreality_id-keyed listing_fetch_failures row.
        pass

    def claimable_count(self, conn: Any) -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM listing_detail_queue "
                "WHERE source = 'ceskereality' AND claimed_at IS NULL AND given_up = false"
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


def _finalize(run_id: int | None, agg: dict[str, Any], *, drain: bool = False) -> None:
    if run_id is None or (not agg and not drain):
        return
    try:
        with db.connect() as conn:
            db.scrape_run_finalize(
                conn, run_id,
                index_pages=agg.get("index_pages", 0),
                listings_found_new=agg.get("listings_found_new", 0),
                listings_scraped_new=agg.get("listings_scraped_new", 0),
                listings_updated=agg.get("listings_updated", 0),
                listings_inactive=agg.get("listings_inactive", 0),
                images_discovered=agg.get("images_discovered", 0),
                images_stored=0,  # crawl records image-URL rows only; bytes uploaded async by images.yml
                errors=agg.get("errors", 0),
                by_category=agg.get("by_category", []),
                bump_already_applied=drain,
            )
    except Exception as exc:
        LOG.warning("scrape_run_finalize failed: %s", exc)


def _run_phase(
    portal: CeskerealityPortal, run_type: str, runner: Any, dry_run: bool, **kw: Any,
) -> int:
    run_id: int | None = None
    if not dry_run:
        try:
            with db.connect() as conn:
                run_id = db.scrape_run_start(conn, run_type, source=SOURCE)
        except Exception as exc:
            LOG.warning("scrape_run_start failed: %s", exc)
    agg: dict[str, Any] = {}
    rc = 0
    try:
        kw = {**kw, "run_id": run_id}
        rc, agg = runner(portal, dry_run=dry_run, **kw)
    finally:
        if not dry_run:
            _finalize(run_id, agg, drain=runner is portal_runner.run_detail_drain)
    return rc


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _configure_logging(args.verbose)

    config = _load_config(args.dry_run)
    regions = tuple(args.region) if args.region else None
    portal = CeskerealityPortal(config, max_pages=args.max_pages, regions=regions)

    # Resolve operational limits: CLI override > per-portal DB config > default.
    workers = args.workers if args.workers is not None else config.limits.detail_workers
    rate = args.rate if args.rate is not None else config.limits.detail_rate
    max_detail = (
        args.max_detail if args.max_detail is not None
        else config.limits.max_detail_per_run
    )

    # ceskereality is mid-sized (~26k listings), so a combined run (omit both
    # --index-only / --drain-only) does the full index walk + a bounded drain in
    # one job. The split flags exist for parity / tuning if it ever outgrows that.
    rc = 0
    if not args.drain_only:
        rc = _run_phase(portal, "index", portal_runner.run_index_walk, args.dry_run)
    if rc == 0 and not args.index_only:
        rc = _run_phase(
            portal, "detail", portal_runner.run_detail_drain, args.dry_run,
            max_claims=max_detail, detail_workers=workers, detail_rate=rate,
            max_seconds=args.max_seconds,
        )
    return rc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ceskereality.cz scraper (portal framework)")
    p.add_argument(
        "--max-pages", type=int, default=None,
        help="cap index pages per category (ad-hoc partial run; suppresses "
             "mark_inactive). Omit for a full, complete walk.",
    )
    p.add_argument(
        "--max-detail", type=int, default=None,
        help="cap detail-drain claims per run (omit = per-portal config / drain the queue)",
    )
    p.add_argument(
        "--workers", type=int, default=None,
        help="detail-fetch workers (default: per-portal config)",
    )
    p.add_argument(
        "--rate", type=float, default=None,
        help="detail-fetch requests/second ceiling (default: per-portal config)",
    )
    p.add_argument(
        "--max-seconds", type=float, default=None,
        help="wall-clock budget for the detail drain; it stops claiming + "
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
    p.add_argument(
        "--region", action="append", default=None,
        help="limit the index walk to these okres/facet slugs (repeatable; e.g. "
             "praha-hlavni-mesto) for an ad-hoc partial proxy test. Suppresses "
             "mark_inactive. Omit = the full okres split.",
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
