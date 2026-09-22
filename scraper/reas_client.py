"""HTTP client for reas.cz's sold-transaction catalogue (sold-comps W2).

Requests only — the payload is read by `scraper.reas_parser`. One page of sold
records is one SSR HTML GET: the `/prodane/...` page carries the whole result set
in `<script id="__NEXT_DATA__">`. The `/_next/data/<buildId>/prodane/...` JSON
route is NEVER used: it loses the `/prodane` rewrite and answers 200 with the
ACTIVE catalogue (no sold price on any record), and it pins a buildId that rotates
on every deploy of the source.
"""

from __future__ import annotations

from scraper.portal_base import BasePortalClient
from scraper.rate_ledger import build_rate_limiter
from scraper.reas_parser import SOURCE

SOLD_LIST_URL = "https://www.reas.cz/prodane/nemovitosti"

# The source honours arbitrary page sizes; 100 is the largest that still returns a
# whole small-town cell in ONE request (Olomouc ±10 km = 89 records, one page).
PAGE_SIZE = 100

# One request per five seconds. Every page is SSR-computed and served
# `cache-control: private, no-cache, no-store`, so a ~1.9 MB list page is real
# origin work rather than an edge hit — the politeness budget is set against that,
# not against our own cost.
RATE_PER_S = 0.2

# A cell is 1–11 requests, so a 20-slot lease would reserve minutes of the shared
# budget the caller never spends.
_LEASE_N = 5


class ReasClient(BasePortalClient):
    """One page of the sold catalogue for one bounding box."""

    ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    # reas.cz is a site we identify ourselves to rather than blend into: product,
    # repo and purpose, no contact data (the `scraper.overpass_client` shape).
    USER_AGENT = (
        "sreality-tracker/0.1 (+https://github.com/waiff/sreality; "
        "sold-transaction comparables)"
    )
    USE_PROXY = False

    def fetch_sold_page(
        self, bounds: tuple[float, float, float, float], *, page: int = 1
    ) -> str:
        """The SSR HTML of one sold-list page for `(sw_lat, sw_lng, ne_lat, ne_lng)`.

        `bounds` is COMMA-SEPARATED and LAT FIRST. The JSON-array form is accepted
        and silently ignored, and the server echoes the box back into transposed
        field names (`southWestLatitude` holding a longitude) — its own internal
        convention, measured, and not ours to mirror.
        """
        sw_lat, sw_lng, ne_lat, ne_lng = bounds
        response = self._request(SOLD_LIST_URL, params={
            "bounds": f"{sw_lat},{sw_lng},{ne_lat},{ne_lng}",
            "listPerPage": PAGE_SIZE,
            "listPage": page,
            "sort": "newest",
        })
        return response.text


def build_client() -> ReasClient:
    """A client on the SHARED politeness ledger, so every runtime spends one budget.

    `portal_rate_state` self-seeds on first use; with no database reachable the
    ledger falls back to per-process pacing at the same rate.
    """
    return ReasClient(
        limiter=build_rate_limiter(SOURCE, RATE_PER_S, shared=True, lease_n=_LEASE_N),
        # ONE retry, not the base class's three. 403 and 429 are RETRYABLE_STATUS, so
        # a page the site is actively refusing would otherwise be asked four times
        # (and `penalize()` four times) — while a sold cell is a 35-day-TTL fact feed
        # whose failures already come back in six hours. Nothing here is urgent
        # enough to justify knocking again after being told no.
        max_retries=1,
    )
