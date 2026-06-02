"""De-risk the auth path: mint a session, hit estate_prices once, report.

Run this FIRST (workflow_dispatch mode=probe) after setting the Seznam
secrets — it proves the login + cookie actually authorize the stats API
before a full ingestion run. No DB writes.

  python -m scripts.price_stats_probe
"""

from __future__ import annotations

import datetime as dt
import logging

from scraper.price_stats_client import AuthExpiredError, PriceStatsClient
from scraper.sreality_auth import get_session_cookies

LOG = logging.getLogger(__name__)

_PROBE_DATASET = {"category_main_cb": 1, "distance": 0}  # byty, no extra filters


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cookies = get_session_cookies(force_login=True)
    LOG.info("minted %d session cookies", len(cookies))
    client = PriceStatsClient(cookies=cookies)

    muni = client.suggest_municipality("Praha")
    if not muni:
        LOG.error("PROBE FAIL: could not resolve 'Praha' via localities/suggest")
        return 2
    LOG.info("resolved Praha -> entity_id=%s obec coords=(%s,%s)",
             muni["entity_id"], muni["lat"], muni["lon"])

    today = dt.date.today()
    start = f"{today.year - 1:04d}-{today.month:02d}"
    end = f"{today.year:04d}-{today.month:02d}"
    try:
        window = client.fetch_window(
            _PROBE_DATASET, entity_id=muni["entity_id"],
            entity_type=muni["entity_type"], category_type_cb=1,
            default_from=start, default_to=end,
        )
    except AuthExpiredError:
        LOG.error("PROBE FAIL: estate_prices returned 401 — the session did "
                  "NOT authorize the stats API. Check the login flow / creds.")
        return 1

    months = window["months"]
    LOG.info("PROBE OK: estate_prices returned %d month(s); latest=%s",
             len(months), months[-1] if months else None)
    LOG.info("aggregates: %s", window["aggregates"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
