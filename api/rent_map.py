"""Ingest the MF rent-map XLSX into rent_map_* (write path).

Parsing + the read-side reference calc live in `toolkit.rent_map`; the write
lives here (out of the read-only toolkit). Shared by `scripts.fetch_rent_map`
and the `/admin/rent-map` upload endpoint so both ingest identically.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import TYPE_CHECKING, Any

import requests

from toolkit.rent_map import (
    ParsedRentMap,
    parse_rent_map_xlsx,
    sha256_bytes,
    source_date_from_filename,
)

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger("rent_map")

MF_INFOGRAPHIC_URL = (
    "https://mf.gov.cz/cs/rozpoctova-politika/podpora-projektoveho-rizeni/"
    "cenova-mapa/cenova-mapa-infografika"
)
_MF_BASE = "https://mf.gov.cz"
_UA = "Mozilla/5.0 (compatible; sreality-rentmap/1.0)"


def find_latest_xlsx_url(page_html: str) -> str | None:
    """The MF page lists the current file + historical ones; the current is
    always the newest date, so pick the max-dated Cenova-mapa .xlsx href."""
    hrefs = re.findall(
        r"""/assets/attachments/[^"'<> ]*?Cenova-mapa[^"'<> ]*?\.xlsx""",
        page_html,
        re.IGNORECASE,
    )
    if not hrefs:
        return None

    def date_key(href: str) -> str:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", href)
        return m.group(1) if m else ""

    best = max(hrefs, key=date_key)
    return best if best.startswith("http") else _MF_BASE + best


def fetch_latest_xlsx(
    source_url: str = MF_INFOGRAPHIC_URL, *, timeout: int = 60,
) -> tuple[bytes, str]:
    """Download the current MF rent-map XLSX. Returns (bytes, filename)."""
    headers = {"User-Agent": _UA}
    page = requests.get(source_url, timeout=timeout, headers=headers)
    page.raise_for_status()
    url = find_latest_xlsx_url(page.text)
    if not url:
        raise ValueError("rent map: no XLSX link found on MF page")
    resp = requests.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    return resp.content, url.rsplit("/", 1)[-1]


def insert_revision(
    conn: "psycopg.Connection",
    parsed: ParsedRentMap,
    *,
    source_filename: str,
    file_sha256: str,
    source_date: date | None,
    uploaded_by: str | None,
) -> int | None:
    """Insert one revision + its values/adjustments, then refresh the matview.

    Returns the new source_revision, or None when this exact file (by
    sha256) was already ingested — re-fetching an unchanged file is a no-op.
    """
    territory_count = len({v.ruian_code for v in parsed.values})
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "select source_revision from rent_map_revisions "
            "where file_sha256 = %s",
            (file_sha256,),
        )
        existing = cur.fetchone()
        if existing is not None:
            LOG.info("rent map sha256 %s already ingested (rev %s)",
                     file_sha256[:12], existing[0])
            return None

        cur.execute(
            """
            insert into rent_map_revisions
              (source_date, source_filename, file_sha256, row_count, uploaded_by)
            values (%s, %s, %s, %s, %s)
            returning source_revision
            """,
            (source_date, source_filename, file_sha256, territory_count,
             uploaded_by),
        )
        (revision,) = cur.fetchone()

        with cur.copy(
            "copy rent_map_values (source_revision, ruian_code, level, kraj, "
            "ku_name, obec_name, vk, ref_rent_per_m2, "
            "ref_rent_novostavba_per_m2, data_coverage) from stdin"
        ) as copy:
            for v in parsed.values:
                copy.write_row((
                    revision, v.ruian_code, v.level, v.kraj, v.ku_name,
                    v.obec_name, v.vk, v.ref_rent_per_m2,
                    v.ref_rent_novostavba_per_m2, v.data_coverage,
                ))

        with cur.copy(
            "copy rent_map_adjustments (source_revision, vk, is_novostavba, "
            "attribute, czk_per_m2) from stdin"
        ) as copy:
            for a in parsed.adjustments:
                copy.write_row((
                    revision, a.vk, a.is_novostavba, a.attribute, a.czk_per_m2,
                ))

        cur.execute("refresh materialized view rent_map_choropleth")

    LOG.info("rent map: ingested revision %d (%d territories, %d adjustments)",
             revision, territory_count, len(parsed.adjustments))
    return revision


def ingest_bytes(
    conn: "psycopg.Connection",
    data: bytes,
    *,
    source_filename: str,
    uploaded_by: str | None,
) -> dict[str, Any]:
    """Parse + ingest an XLSX byte blob. Returns a small status dict."""
    parsed = parse_rent_map_xlsx(
        data, source_date=source_date_from_filename(source_filename)
    )
    file_sha256 = sha256_bytes(data)
    revision = insert_revision(
        conn, parsed,
        source_filename=source_filename,
        file_sha256=file_sha256,
        source_date=parsed.source_date,
        uploaded_by=uploaded_by,
    )
    return {
        "ingested": revision is not None,
        "source_revision": revision,
        "source_date": parsed.source_date.isoformat() if parsed.source_date else None,
        "source_filename": source_filename,
        "file_sha256": file_sha256,
        "territory_count": len({v.ruian_code for v in parsed.values}),
        "adjustment_count": len(parsed.adjustments),
    }


def current_revision(conn: "psycopg.Connection") -> dict[str, Any] | None:
    """Summary of the latest ingested revision, or None if none yet."""
    with conn.cursor() as cur:
        cur.execute(
            "select source_revision, source_date, source_filename, row_count, "
            "uploaded_by, uploaded_at from rent_map_revisions "
            "order by source_revision desc limit 1"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "source_revision": row[0],
        "source_date": row[1].isoformat() if row[1] else None,
        "source_filename": row[2],
        "row_count": row[3],
        "uploaded_by": row[4],
        "uploaded_at": row[5].isoformat() if row[5] else None,
    }


def list_revisions(
    conn: "psycopg.Connection", *, limit: int = 50
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "select source_revision, source_date, source_filename, row_count, "
            "uploaded_by, uploaded_at from rent_map_revisions "
            "order by source_revision desc limit %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "source_revision": r[0],
            "source_date": r[1].isoformat() if r[1] else None,
            "source_filename": r[2],
            "row_count": r[3],
            "uploaded_by": r[4],
            "uploaded_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]
