"""HTTP layer for bezrealitky.cz (multi-portal portal framework).

Bezrealitky is a Next.js SPA backed by a public GraphQL API at
`api.bezrealitky.cz/graphql/`, so — like sreality's JSON v1 API and unlike the
bazos HTML crawler — this returns parsed JSON for `scraper.bezrealitky_parser`,
not raw HTML. The shared retry/backoff + adaptive throttle (`RateLimiter` +
`penalize()` on 429/403) live in `scraper.portal_base.BasePortalClient`; this
client adds only the JSON `Accept`, the Origin/Referer the API requires, the two
GraphQL queries (search index + single-advert detail), and the
`advert.active == false` (or a null advert) -> `ListingGoneError` delisting signal.

`includeImports=false` scopes the walk to bezrealitky's OWN (private-seller)
inventory — the unique value-add — and leaves any overlap with imported/other-
portal listings to the cross-source dedup engine.
"""

from __future__ import annotations

import hashlib
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
query Index($ot: [OfferType], $et: [EstateType], $inc: Boolean, $st: Boolean, $lim: Int, $off: Int) {
  listAdverts(
    offerType: $ot, estateType: $et,
    includeImports: $inc, includeShortTerm: $st,
    limit: $lim, offset: $off,
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
    ruianId
    addressInput
    regionTree(locale: CS) { id lvl type subType name }
    construction condition ownership equipped penb
    etage totalFloors
    parking garage lift
    active timeActivated timeDeactivated
    mainImage { url(filter: RECORD_MAIN) }
    publicImages { url(filter: RECORD_MAIN) order }
  }
}
"""


_DETAIL_QUERY_SHA256 = hashlib.sha256(_DETAIL_QUERY.encode("utf-8")).hexdigest()


def detail_url(uri: str) -> str:
    """Public listing URL for a bezrealitky advert `uri`."""
    return f"{BASE_URL}/nemovitosti-byty-domy/{uri}"


def detail_payload_body(advert: dict[str, Any]) -> dict[str, Any]:
    """The archivable form of one detail response (02 section 2.3.2 P3).

    A GraphQL payload is only as wide as the query that asked for it, so the
    archive stores the exact query text and its sha256 beside the data —
    otherwise "what could this row possibly contain" is answerable only by
    archaeology, and a field the query never requested reads as absent from the
    portal. It lives here, next to the query, so editing the field list cannot
    silently desync the archived query from the archived data.
    """
    return {
        "query": _DETAIL_QUERY,
        "query_sha256": _DETAIL_QUERY_SHA256,
        "data": advert,
    }


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
        include_imports: bool = True, include_short_term: bool = True,
    ) -> tuple[list[dict[str, Any]], int]:
        """One index page: returns (adverts, total_count).

        `estate_type` accepts either a single enum string or a list of them, so
        a portals config descriptor can group several estate types that
        canonicalise to the same `category_main` (e.g. KANCELAR +
        NEBYTOVY_PROSTOR both → 'komercni') into ONE walk — required so the
        source-scoped `mark_inactive` (which keys on canonical cm/ct) sees the
        union of seen ids, not two disjoint subsets that would mutually delist.

        `include_imports` defaults to True (the listAdverts API default + what
        bezrealitky.cz shows in its CZ-scoped count), bringing aggregator-imported
        CZ listings — cross-source duplicates are collapsed by the dedup engine.
        Set False per-descriptor when a category's imports are aggregator noise,
        e.g. PRONAJEM/REKREACNI_OBJEKT is ~7000 vacation rentals.
        """
        et = list(estate_type) if isinstance(estate_type, list) else [estate_type]
        data = self._graphql(
            _INDEX_QUERY,
            {"ot": [offer_type], "et": et,
             "inc": include_imports, "st": include_short_term,
             "lim": limit, "off": offset},
        )
        result = data.get("listAdverts") or {}
        return list(result.get("list") or []), int(result.get("totalCount") or 0)

    def get_detail(self, advert_id: str) -> dict[str, Any]:
        """Full advert object. Raises `ListingGoneError` only on a POSITIVE
        signal: the API answered and said the advert is null, or returned it
        with `active: false`. The second is the one the portal actually sends
        (verified against the live API 2026-09-08): a withdrawn advert comes
        back as a full record with active=false -- timeDeactivated set or not
        -- and an UNKNOWN id as a stub with active=false and an empty title.
        The null advert this used to wait for never arrives, which is why the
        portal closed 115-159 listings a day until the presence-check cutover
        and zero after it: 323 checks in a row read active=false as alive.

        Everything else is an ERROR, not gone: no `advert` key at all (an empty
        `data`, an edge stub, a partial outage) and an advert whose `active` is
        missing or null (the shape a field takes when the API denies access to
        it). Since 2026-09-07 every unseen active row is checked this way
        (rule #3), so reading "no answer" as "gone" would delist a whole
        nomination batch during one bad minute."""
        data = self._graphql(_DETAIL_QUERY, {"id": str(advert_id)})
        if "advert" not in data:
            raise RuntimeError(
                f"bezrealitky detail {advert_id}: response carried no 'advert' field"
            )
        advert = data["advert"]
        if advert is None:
            raise ListingGoneError(detail_url(str(advert_id)), None)
        active = advert.get("active")
        if active is False:
            raise ListingGoneError(detail_url(str(advert.get("uri") or advert_id)), None)
        if active is not True:
            raise RuntimeError(
                f"bezrealitky detail {advert_id}: advert carried no usable "
                f"'active' flag ({active!r})"
            )
        return advert
