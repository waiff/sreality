"""GET /properties/{property_id}/download.zip — the property page's download button.

One zip: a PDF of the ad as the page shows it (api/property_sheet.py) plus the canonical
ad's stored photos in gallery order, the ones the page's gallery shows. Built here because
the R2 bucket sends no CORS header, so the SPA cannot read the bytes behind /images/{key}.
Listing data only: nothing the operator curated (tags, notes, collections, pipeline) is read.
Signed-in users only, since it proxies photo bytes through us; a broker's phone and e-mail
follow the /brokers/* PII policy (values for an admin, "hidden" for anyone else).
"""

from __future__ import annotations

import io
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from psycopg.rows import dict_row

from api import dependencies as deps
from api.property_sheet import render_property_sheet
from api.routes.images import r2_client
from toolkit import brokers

router = APIRouter(tags=["properties"])

# properties_public also carries broker_name / broker_email / broker_phone: never read them
# here — brokers come through listing_broker_public and the PII policy below.
_PROPERTY_COLS = (
    "property_id, listing_id, is_active, last_seen_at, category_main, category_type,"
    " price_czk, price_unit, area_m2, disposition, subtype, display_label, lat, lng,"
    " floor, total_floors, has_balcony, terrace, has_lift, cellar, garage, has_parking,"
    " parking_lots, building_type, condition, energy_rating, estate_area, garden_area,"
    " ownership, furnished, description"
)

# Parallel R2 reads: a 40-photo listing fetched serially spends seconds on round trips alone.
_ZIP_FETCH_WORKERS = 8


def property_brokers(
    ads: list[dict[str, Any]], by_listing: dict[int, dict[str, Any]], canonical_id: int
) -> tuple[list[dict[str, Any]], bool]:
    """lib/brokers.propertyBrokers: the brokers of the active ads, one per person, the
    canonical ad's first; with no active ad, those of all its ads, flagged."""
    active = [a for a in ads if a["is_active"]]
    pool = active or ads
    ordered = sorted(pool, key=lambda a: a["id"] != canonical_id)
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for ad in ordered:
        b = by_listing.get(ad["id"])
        if b is None or b["broker_id"] in seen:
            continue
        seen.add(b["broker_id"])
        out.append(b)
    return out, not active


@router.get("/properties/{property_id}/download.zip")
def get_property_download(
    property_id: int,
    conn: Any = Depends(deps.get_db_conn),
    claims: dict = Depends(deps.verify_jwt),
) -> Response:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"select {_PROPERTY_COLS} from properties_public where property_id = %s",
            (property_id,),
        )
        prop = cur.fetchone()
        if prop is None:
            raise HTTPException(status_code=404, detail="Property not found")
        cur.execute(
            "select id, source, source_url, is_active, price_czk, first_seen_at"
            " from property_sources_public where property_id = %s"
            " order by first_seen_at, id",
            (property_id,),
        )
        ads = cur.fetchall()
        cur.execute(
            "select storage_path from images"
            " where listing_id = %s and storage_path is not null"
            " order by sequence nulls last, id",
            (prop["listing_id"],),
        )
        keys = [r["storage_path"] for r in cur.fetchall()]

    ad_ids = [a["id"] for a in ads] or [prop["listing_id"]]
    rows = brokers.apply_pii_policy(
        brokers.listing_brokers(conn, ad_ids), include_pii=deps.is_admin(claims)
    )["data"]
    shown, from_inactive = property_brokers(
        ads, {r["listing_id"]: r for r in rows}, prop["listing_id"]
    )

    photos: list[bytes] = []
    if keys:
        client = r2_client()
        if client is None:
            raise HTTPException(status_code=503, detail="Image storage not configured")
        try:
            with ThreadPoolExecutor(max_workers=_ZIP_FETCH_WORKERS) as pool:
                photos = list(pool.map(client.download_bytes, keys))
        except Exception as exc:
            # A half-filled zip would pass for the full set.
            raise HTTPException(
                status_code=502, detail="A photo could not be read from storage"
            ) from exc

    sheet = render_property_sheet(
        prop,
        ads,
        shown,
        brokers_from_inactive=from_inactive,
        cover=photos[0] if photos else None,
        today=datetime.now(UTC).date(),
    )
    width = max(2, len(str(len(photos))))
    buf = io.BytesIO()
    # Stored, not deflated: JPEG bytes don't compress and the PDF's streams already are.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(f"property-{property_id}.pdf", sheet)
        for n, data in enumerate(photos, start=1):
            archive.writestr(f"{n:0{width}d}.jpg", data)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="property-{property_id}.zip"'},
    )
