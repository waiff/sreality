"""HTTP layer for the OpenStreetMap Overpass API.

Fetches POI elements for a (lat, lng, radius_m) circle filtered by a
list of OSM tag dicts. Mirrors the structure of sreality_client.py:
polite throttling, retries on the usual transient codes, browser-ish
User-Agent.

Tag mapping is NOT in this module. Callers (toolkit/amenities.py)
translate a category name to a list of tag dicts and pass that in;
this module only knows how to render Overpass QL and parse responses.

Element types queried per filter: nodes, ways, relations. Ways and
relations are returned with `out center` so we always get a single
point per match — same shape as a node element.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

LOG = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

DEFAULT_USER_AGENT = (
    "sreality-tracker/0.1 (+https://github.com/waiff/sreality; OSM amenity cache)"
)

RETRYABLE_STATUS: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
)


class OverpassClient:
    def __init__(
        self,
        request_delay_s: float = 2.0,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
        url: str = OVERPASS_URL,
    ) -> None:
        self.request_delay_s = request_delay_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.url = url
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": user_agent,
            "Accept": "application/json",
        })
        self._last_request_at = 0.0

    def fetch(
        self,
        category_tags: list[dict[str, str | bool]],
        lat: float,
        lng: float,
        radius_m: int,
    ) -> list[dict[str, Any]]:
        """Fetch parsed POI elements matching any of the tag-filter dicts.

        `category_tags` is a list of tag dicts; OR semantics across the
        list, AND semantics within each dict. A value of True means
        "key present, any value".

        Returns one normalized dict per element, shape:
          {
            "source_id": "node/12345" | "way/67890" | "relation/...",
            "name": str | None,
            "lat": float,
            "lng": float,
            "tags": dict[str, str],
          }
        """
        if not category_tags:
            return []
        body = _build_query(category_tags, lat, lng, radius_m)
        self._throttle()
        payload = self._post(body)
        self._last_request_at = time.monotonic()
        return _parse_elements(payload.get("elements", []))

    def fetch_routes(
        self,
        transport_types: list[str],
        bbox_minlat: float,
        bbox_minlng: float,
        bbox_maxlat: float,
        bbox_maxlng: float,
    ) -> list[dict[str, Any]]:
        """Fetch route relations of the given transport types in a bbox.

        Returns one row per (relation, member way) pair. Each row carries
        the relation's metadata (route_ref, name, transport_type) plus
        the polyline coordinates of the member way. Splitting at the way
        level avoids the merge ambiguity that bites if a relation has
        branches or loops — every way is a clean polyline.

        Element shape:
          {
            "source_id":      "relation/R/way/W",
            "transport_type": "tram" | "subway" | "bus",
            "route_ref":      str | None,   # e.g. "9", "A", "112"
            "name":           str | None,
            "linestring":     [(lat, lng), ...],   # at least 2 points
            "tags":           dict[str, str],
          }
        """
        if not transport_types:
            return []
        body = _build_route_query(
            transport_types, bbox_minlat, bbox_minlng,
            bbox_maxlat, bbox_maxlng,
        )
        self._throttle()
        payload = self._post(body)
        self._last_request_at = time.monotonic()
        return _parse_route_elements(payload.get("elements", []))

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_delay_s:
            time.sleep(self.request_delay_s - elapsed)

    def _post(self, body: str) -> dict[str, Any]:
        error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                time.sleep(2.0 ** (attempt - 1))
            try:
                response = self._session.post(
                    self.url, data={"data": body}, timeout=self.timeout_s,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                error = exc
                LOG.warning(
                    "POST %s attempt %d/%d failed: %s",
                    self.url, attempt + 1, self.max_retries + 1, exc,
                )
                continue
            if response.status_code in RETRYABLE_STATUS:
                error = requests.HTTPError(
                    f"{response.status_code} from {self.url}",
                    response=response,
                )
                LOG.warning(
                    "POST %s attempt %d/%d failed: %s",
                    self.url, attempt + 1, self.max_retries + 1, error,
                )
                continue
            if response.status_code >= 400:
                response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                error = exc
                LOG.warning(
                    "POST %s attempt %d/%d JSON decode failed: %s",
                    self.url, attempt + 1, self.max_retries + 1, exc,
                )
        assert error is not None
        raise error


def _build_query(
    category_tags: list[dict[str, str | bool]],
    lat: float,
    lng: float,
    radius_m: int,
) -> str:
    """Render Overpass QL for the union of (element_type × tag_filter)."""
    around = f"around:{radius_m},{lat},{lng}"
    parts: list[str] = []
    for tags in category_tags:
        filt = "".join(_render_tag(k, v) for k, v in tags.items())
        for el_type in ("node", "way", "relation"):
            parts.append(f"  {el_type}{filt}({around});")
    return "[out:json][timeout:25];\n(\n" + "\n".join(parts) + "\n);\nout center tags;"


def _render_tag(key: str, value: str | bool) -> str:
    """Render one [key=value] / [key] / [key~value] filter clause."""
    if value is True:
        return f'["{_esc(key)}"]'
    return f'["{_esc(key)}"="{_esc(str(value))}"]'


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


_TRANSPORT_TYPES_ALLOWED: frozenset[str] = frozenset({"tram", "subway", "bus"})


def _build_route_query(
    transport_types: list[str],
    bbox_minlat: float,
    bbox_minlng: float,
    bbox_maxlat: float,
    bbox_maxlng: float,
) -> str:
    """Render Overpass QL for route relations of the given transport types.

    Bbox order in Overpass QL is (south, west, north, east) =
    (minlat, minlng, maxlat, maxlng). `out geom;` returns member way
    geometries inline as lat/lon arrays.
    """
    bbox = f"{bbox_minlat},{bbox_minlng},{bbox_maxlat},{bbox_maxlng}"
    parts = [
        f'  relation["type"="route"]["route"="{_esc(tt)}"]({bbox});'
        for tt in transport_types
    ]
    return "[out:json][timeout:60];\n(\n" + "\n".join(parts) + "\n);\nout geom;"


def _parse_route_elements(
    elements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for el in elements:
        if el.get("type") != "relation":
            continue
        rel_id = el.get("id")
        if rel_id is None:
            continue
        tags = el.get("tags") or {}
        transport_type = tags.get("route")
        if transport_type not in _TRANSPORT_TYPES_ALLOWED:
            continue
        route_ref = tags.get("ref")
        name = tags.get("name")
        members = el.get("members") or []
        for member in members:
            if member.get("type") != "way":
                continue
            way_id = member.get("ref")
            if way_id is None:
                continue
            geom_pts = member.get("geometry") or []
            if len(geom_pts) < 2:
                continue
            coords: list[tuple[float, float]] = []
            for pt in geom_pts:
                lat = pt.get("lat")
                lng = pt.get("lon")
                if lat is None or lng is None:
                    continue
                coords.append((float(lat), float(lng)))
            if len(coords) < 2:
                continue
            out.append({
                "source_id":      f"relation/{rel_id}/way/{way_id}",
                "transport_type": transport_type,
                "route_ref":      route_ref,
                "name":           name,
                "linestring":     coords,
                "tags":           tags,
            })
    return out


def _parse_elements(elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for el in elements:
        el_type = el.get("type")
        el_id = el.get("id")
        if el_type is None or el_id is None:
            continue
        if el_type == "node":
            lat = el.get("lat")
            lng = el.get("lon")
        else:
            center = el.get("center") or {}
            lat = center.get("lat")
            lng = center.get("lon")
        if lat is None or lng is None:
            continue
        tags = el.get("tags") or {}
        out.append({
            "source_id": f"{el_type}/{el_id}",
            "name": tags.get("name"),
            "lat": float(lat),
            "lng": float(lng),
            "tags": tags,
        })
    return out
