"""Entrypoint for one reas.cz sold-transaction cell.

  python -m scraper.reas_main --obec 554782              # fetch, store, record
  python -m scraper.reas_main --obec 554782 --dry-run    # box + walk, no writes
  python -m scraper.reas_main --bbox 49.5,17.1,49.7,17.4 --dry-run   # no DB at all

The `--bbox` form takes no obec, so it has no cell identity and therefore no ledger
row: it is the smoke test, and it refuses to run without `--dry-run`.
"""

from __future__ import annotations

import argparse
import logging

from scraper import reas_client, sold_db, sold_fetch
from scraper.reas_parser import SoldTransaction

LOG = logging.getLogger(__name__)


def _parse_bbox(text: str) -> tuple[float, float, float, float]:
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("--bbox takes swLat,swLng,neLat,neLng")
    try:
        sw_lat, sw_lng, ne_lat, ne_lng = (float(p) for p in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--bbox is not four numbers: {exc}") from exc
    return (sw_lat, sw_lng, ne_lat, ne_lng)


def _report(
    rows: list[SoldTransaction], pages: int, source_total: int | None, dropped: int
) -> None:
    LOG.info(
        "SOLD preview records=%d pages=%d source_total=%s dropped=%d",
        len(rows), pages, source_total, dropped,
    )
    for row in rows[:3]:
        LOG.info(
            "  %s %s %s %s %s Kč %s m² (%s) %s",
            row.source_record_id, row.sold_at, row.category_main,
            row.disposition or "-", row.price_czk, row.area_m2, row.area_basis,
            row.address_text,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch one reas.cz sold cell")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--obec", type=int, help="RÚIAN obec kód")
    target.add_argument("--bbox", type=_parse_bbox, help="swLat,swLng,neLat,neLng")
    parser.add_argument("--max-pages", type=int, default=sold_fetch.MAX_PAGES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.bbox and not args.dry_run:
        parser.error("--bbox has no cell to record; pass --dry-run")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    client = reas_client.build_client()

    if args.bbox:
        _report(*sold_fetch.walk_bounds(client, args.bbox, max_pages=args.max_pages))
        return 0

    conn = sold_db.connect()
    if args.dry_run:
        box = sold_db.obec_cell_box(conn, args.obec)
        if box is None:
            LOG.error("obec %s has no boundary polygon", args.obec)
            return 1
        bounds = (box.sw_lat, box.sw_lng, box.ne_lat, box.ne_lng)
        LOG.info("SOLD cell obec=%s bounds=%s", args.obec, bounds)
        _report(*sold_fetch.walk_bounds(client, bounds, max_pages=args.max_pages))
        return 0

    result = sold_fetch.fetch_cell(
        conn, client, args.obec, max_pages=args.max_pages
    )
    LOG.info(
        "SOLD cell obec=%s status=%s records=%d new=%d pages=%d source_total=%s "
        "dropped=%d %ss %s",
        result["obec_kod"], result["status"], result["records"], result["new"],
        result["pages"], result["source_total"], result["dropped"],
        result["seconds"], result["error"] or "",
    )
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
