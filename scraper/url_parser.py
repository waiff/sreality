"""URL → sreality detail spec.

Extends scraper.parser by accepting a sreality.cz URL, extracting the
sreality_id, fetching the detail JSON via the existing client, and
returning the parsed spec ready to feed an estimation. Reuses
scraper.parser verbatim — no duplicated parsing logic here.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from scraper import parser

if TYPE_CHECKING:
    import psycopg

    from scraper.sreality_client import SrealityClient


_ID_RE = re.compile(r"/(\d{8,})(?=$|[/?#])")


def extract_sreality_id(url: str) -> int:
    """Return the trailing 8+-digit path segment of url. Raises ValueError if none."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("empty or non-string url")
    matches = _ID_RE.findall(url)
    if not matches:
        raise ValueError(f"no sreality_id found in url: {url!r}")
    return int(matches[-1])


def parse_sreality_url(
    url: str,
    *,
    client: "SrealityClient",
    conn: "psycopg.Connection",
) -> dict[str, Any]:
    """Resolve a sreality.cz URL to a parsed spec ready for estimation.

    Raises ValueError if no sreality_id can be recovered from the URL.
    Lets requests.HTTPError from the underlying client propagate.
    """
    sreality_id = extract_sreality_id(url)
    raw = client.get_detail(sreality_id)
    spec = parser.parse_listing(raw)
    images = parser.parse_images(raw)
    return {
        "sreality_id": sreality_id,
        "spec": spec,
        "images": images,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_url": url,
        "in_database": _exists_in_database(conn, sreality_id),
    }


def _exists_in_database(
    conn: "psycopg.Connection", sreality_id: int
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM listings WHERE sreality_id = %s LIMIT 1",
            (sreality_id,),
        )
        return cur.fetchone() is not None
