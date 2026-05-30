"""HTTP layer for bezrealitky.cz (multi-portal portal framework).

Bezrealitky is a Next.js SPA backed by a public GraphQL API at
`api.bezrealitky.cz/graphql/`, so — like sreality's JSON v1 API and unlike the
bazos HTML crawler — this returns parsed JSON for `scraper.bezrealitky_parser`,
not raw HTML. The shared retry/backoff + adaptive throttle (`RateLimiter` +
`penalize()` on 429/403) live in `scraper.portal_base.BasePortalClient`; this
client adds only the JSON `Accept`, the Origin/Referer the API requires, the two
GraphQL queries (search index + single-advert detail), and the
`advert == null -> ListingGoneError` delisting signal.

`includeImports=false` scopes the walk to bezrealitky's OWN (private-seller)
inventory — the unique value-add — and leaves any overlap with imported/other-
portal listings to the cross-source dedup engine.
"""

from __future__ import annotations

import logging
from typing import Any

from scraper.portal_base import BasePortalClient, ListingGoneError

LOG = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.bezrealitky.cz/graphql/"
BASE_URL = "https://www.bezrealitky.cz"

# Only the fields the index walk needs: the id + price (price-change refetch
# decision) + uri (the public detail URL). Cheap page; full data comes from the
# detail query.
_INDEX_QUERY = """
query Index($ot: [OfferType], $et: [EstateType], $lim: Int, $off: Int) {
  listAdverts(
    offerType: $ot, estateType: $et,
    includeImports: false, limit: $lim, offset: $off,
    order: TIMEORDER_DESC, locale: CS
  ) {
    totalCount
    list { id price uri }
  }
}
"""

# The full advert object the parser maps onto a ScrapedListing.
_DETAIL_QUERY = """
query Detail($id: ID!) {
  advert(id: $id) {
    id uri title description
    offerType estateType disposition
    price currency charges originalPrice isDiscounted
    surface surfaceLand frontGarden
    balconySurface terraceSurface cellarSurface loggiaSurface
    gps { lat lng }
    address(locale: CS) street houseNumber
    city(locale: CS) cityDistrict(locale: CS) zip
    construction condition ownership equipped penb
    etage totalFloors
    parking garage lift
    active timeActivated timeDeactivated
    mainImage { url(filter: RECORD_MAIN) }
    publicImages { url(filter: RECORD_MAIN) order }
  }
}
"""


def detail_url(uri: str) -> str:
    """Public listing URL for a bezrealitky advert `uri`."""
    return f"{BASE_URL}/nemovitosti-byty-domy/{uri}"


class BezrealitkyClient(BasePortalClient):
    ACCEPT = "application/json"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # The API 403s a request without a browser-like Origin/Referer.
        self._session.headers.update(
            {"Origin": BASE_URL, "Referer": f"{BASE_URL}/"}
        )

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        response = self._request(
            GRAPHQL_URL, json_body={"query": query, "variables": variables}
        )
        body = response.json()
        if body.get("errors"):
            raise RuntimeError(f"graphql errors: {body['errors']}")
        return body.get("data") or {}

    def search(
        self, offer_type: str, estate_type: str | list[str], *,
        limit: int, offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """One index page: returns (adverts, total_count).

        `estate_type` accepts either a single enum string or a list of them, so
        a portals config descriptor can group several estate types that
        canonicalise to the same `category_main` (e.g. KANCELAR +
        NEBYTOVY_PROSTOR both → 'komercni') into ONE walk — required so the
        source-scoped `mark_inactive` (which keys on canonical cm/ct) sees the
        union of seen ids, not two disjoint subsets that would mutually delist.
        """
        et = list(estate_type) if isinstance(estate_type, list) else [estate_type]
        data = self._graphql(
            _INDEX_QUERY,
            {"ot": [offer_type], "et": et, "lim": limit, "off": offset},
        )
        result = data.get("listAdverts") or {}
        return list(result.get("list") or []), int(result.get("totalCount") or 0)

    def get_detail(self, advert_id: str) -> dict[str, Any]:
        """Full advert object. Raises `ListingGoneError` when the API returns a
        null advert (delisted / unknown id)."""
        data = self._graphql(_DETAIL_QUERY, {"id": str(advert_id)})
        advert = data.get("advert")
        if advert is None:
            raise ListingGoneError(detail_url(str(advert_id)), None)
        return advert
