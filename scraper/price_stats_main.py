"""Ingestion entrypoint for price-stats datasets.

Resolves the configured municipalities (localities/suggest, cached), then for
each active dataset × locality × {prodej, pronájem} fetches the full monthly
series from estate_prices and upserts it, recomputes the per-city derived
metrics, and refreshes the map choropleth. Pure HTTP fetch + psycopg writes;
the session cookie comes from `scraper.sreality_auth`.

  python -m scraper.price_stats_main                 # all active datasets
  python -m scraper.price_stats_main --dataset-id 3  # one dataset
  python -m scraper.price_stats_main --resolve-only  # just refresh localities
  python -m scraper.price_stats_main --dry-run       # no writes
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any

from scraper import price_stats_db as db
from scraper.price_stats_client import AuthExpiredError, PriceStatsClient
from scraper.rate_limit import RateLimiter
from scraper.sreality_auth import get_session_cookies

LOG = logging.getLogger(__name__)

_DEFAULT_CITIES = Path(__file__).resolve().parents[1] / "data" / "price_stat_cities.json"
CATEGORIES = (1, 2)  # prodej, pronajem — every dataset covers both


def _load_city_names(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    cities = data.get("cities") if isinstance(data, dict) else data
    return [str(c).strip() for c in cities if str(c).strip()]


_PERIOD_STEP = {"quarterly": 3, "semiannual": 6, "annual": 12}


def _downsample(
    months: list[dict[str, Any]], periodicity: str | None
) -> list[dict[str, Any]]:
    """Keep one month per period bucket (the last/period-end month present).

    Buckets are calendar-aligned (Jan starts a quarter/half/year since
    year*12 is divisible by 3/6/12). The growth RPC annualizes from the actual
    (year, month) indices, so sampling at any spacing keeps CAGR correct.
    """
    step = _PERIOD_STEP.get(periodicity or "monthly")
    if step is None:  # monthly (or unknown) → keep everything
        return months
    by_bucket: dict[int, dict[str, Any]] = {}
    for m in months:
        ymi = int(m["year"]) * 12 + (int(m["month"]) - 1)
        bucket = ymi // step
        cur = by_bucket.get(bucket)
        if cur is None or ymi > int(cur["year"]) * 12 + (int(cur["month"]) - 1):
            by_bucket[bucket] = m
    return [by_bucket[b] for b in sorted(by_bucket)]


def _parse_ym(s: str | None) -> tuple[int, int] | None:
    if not s:
        return None
    try:
        year, month = str(s).split("-")
        return (int(year), int(month))
    except (ValueError, AttributeError):
        return None


def _dataset_window(
    dataset: dict[str, Any],
    default_start: tuple[int, int],
    default_end: tuple[int, int],
) -> tuple[tuple[int, int], tuple[int, int]]:
    """Per-dataset scrape window, falling back to the CLI/today defaults."""
    return (
        _parse_ym(dataset.get("start_ym")) or default_start,
        _parse_ym(dataset.get("end_ym")) or default_end,
    )


def _dataset_localities(
    conn: Any,
    dataset: dict[str, Any],
    global_localities: list[dict[str, Any]],
    *,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """Localities for a dataset: its selected obce, else the global list."""
    obec_ids = dataset.get("obec_ids")
    if obec_ids:
        ids = [int(x) for x in obec_ids]
        if not dry_run:
            db.resolve_obce(conn, ids)
        return db.localities_for_obec_ids(conn, ids)
    return global_localities


def resolve_localities(
    conn: Any, client: PriceStatsClient, city_names: list[str], *, dry_run: bool
) -> int:
    """Resolve any not-yet-cached city names to municipality entities."""
    known = {row["name"].casefold() for row in db.list_localities(conn)}
    added = 0
    for name in city_names:
        if name.casefold() in known:
            continue
        match = client.suggest_municipality(name)
        if not match or match.get("entity_id") is None:
            LOG.warning("SUGGEST miss city=%r", name)
            continue
        LOG.info("SUGGEST city=%r -> entity_id=%s", name, match["entity_id"])
        if not dry_run:
            db.upsert_locality(conn, match)
            known.add((match.get("name") or name).casefold())
        added += 1
    return added


def run_dataset(
    conn: Any,
    client: PriceStatsClient,
    dataset: dict[str, Any],
    localities: list[dict[str, Any]],
    *,
    start_ym: tuple[int, int],
    end_ym: tuple[int, int],
    window_years: int,
    chunk_months: int,
    dry_run: bool,
) -> None:
    run_id = 0 if dry_run else db.start_run(
        conn, dataset["id"], cities_total=len(localities)
    )
    total_obs = 0
    try:
        for i, loc in enumerate(localities, start=1):
            for category_type_cb in CATEGORIES:
                series = _fetch_with_auth_retry(
                    client, dataset, loc, category_type_cb,
                    start_ym=start_ym, end_ym=end_ym, chunk_months=chunk_months,
                )
                months = _downsample(series["months"], dataset.get("periodicity"))
                LOG.info(
                    "SERIES dataset=%s city=%s ct=%d months=%d kept=%d period=%s",
                    dataset["id"], loc["name"], category_type_cb,
                    len(series["months"]), len(months),
                    dataset.get("periodicity") or "monthly",
                )
                if not dry_run and months:
                    total_obs += db.upsert_observations(
                        conn,
                        dataset_id=dataset["id"],
                        entity_type=loc["entity_type"],
                        entity_id=loc["entity_id"],
                        category_type_cb=category_type_cb,
                        months=months,
                        run_id=run_id,
                    )
            if not dry_run:
                db.update_run_progress(
                    conn, run_id, cities_done=i, observations=total_obs
                )
        if not dry_run:
            metrics = db.recompute_metrics(
                conn, dataset["id"], window_years=window_years
            )
            db.finish_run(
                conn, run_id, status="success",
                localities=len(localities), observations=total_obs,
            )
            LOG.info(
                "DATASET done id=%s observations=%d cities=%d metrics=%d",
                dataset["id"], total_obs, len(localities), metrics,
            )
    except Exception as exc:
        if not dry_run:
            db.finish_run(conn, run_id, status="failed", error=str(exc)[:2000])
        raise


def _fetch_with_auth_retry(
    client: PriceStatsClient,
    dataset: dict[str, Any],
    loc: dict[str, Any],
    category_type_cb: int,
    **kw: Any,
) -> dict[str, Any]:
    try:
        return client.fetch_series(
            dataset, entity_id=loc["entity_id"],
            entity_type=loc["entity_type"], category_type_cb=category_type_cb, **kw
        )
    except AuthExpiredError:
        LOG.warning("session expired mid-run; re-minting cookie")
        client.set_cookies(get_session_cookies(force_login=True))
        return client.fetch_series(
            dataset, entity_id=loc["entity_id"],
            entity_type=loc["entity_type"], category_type_cb=category_type_cb, **kw
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape sreality price stats")
    parser.add_argument("--dataset-id", type=int, default=None)
    parser.add_argument("--cities-file", type=Path, default=_DEFAULT_CITIES)
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--window-years", type=int, default=5)
    parser.add_argument("--chunk-months", type=int, default=24)
    parser.add_argument("--rate", type=float, default=2.0, help="requests/sec")
    parser.add_argument("--resolve-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    today = dt.date.today()
    start_ym = (args.start_year, 1)
    end_ym = (today.year, today.month)

    conn = db.connect()
    cookies = get_session_cookies()
    client = PriceStatsClient(cookies=cookies, limiter=RateLimiter(args.rate))

    city_names = _load_city_names(args.cities_file)
    added = resolve_localities(conn, client, city_names, dry_run=args.dry_run)
    LOG.info("RESOLVE added=%d total_cities=%d", added, len(city_names))
    if args.resolve_only:
        return 0

    global_localities = db.list_localities(conn)

    if args.dataset_id is not None:
        ds = db.get_dataset(conn, args.dataset_id)
        datasets = [ds] if ds else []
    else:
        datasets = db.load_active_datasets(conn)
    if not datasets:
        LOG.warning("no active datasets")
        return 0

    for dataset in datasets:
        locs = _dataset_localities(conn, dataset, global_localities, dry_run=args.dry_run)
        if not locs:
            LOG.warning("dataset %s has no localities to fetch", dataset["id"])
            continue
        ds_start, ds_end = _dataset_window(dataset, start_ym, end_ym)
        run_dataset(
            conn, client, dataset, locs,
            start_ym=ds_start, end_ym=ds_end,
            window_years=args.window_years, chunk_months=args.chunk_months,
            dry_run=args.dry_run,
        )

    if not args.dry_run:
        db.refresh_choropleth(conn)
        LOG.info("CHOROPLETH refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
