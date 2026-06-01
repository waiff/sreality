"""Public image-redirect route: GET /images/{key} -> 302 to a presigned R2 URL.

The frontend serves every listing photo through this endpoint instead of the
sreality CDN, whose tokenised URLs expire within weeks (any stored *.sdn.cz URL
404s once sreality rotates the render token). R2 holds the durable copy; this
route mints a short-lived presigned GET so a *private* bucket can stream bytes
straight to the browser without proxying them through us.

Unauthenticated (like /health) — listing photos are public data and an <img>
tag can't send a bearer header. The key is constrained to the listing-image
shape `-?<id>/<seq>.jpg` so this can never presign the operator-private
`custom-attachments/` building uploads that share the bucket.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from scraper import image_storage

router = APIRouter(prefix="/images", tags=["images"])

# `{native_id}/{seq:04d}.jpg`; native_id is negative for non-sreality portals.
_KEY_RE = re.compile(r"^-?\d+/\d{4}\.jpg$")

# Presign lifetime (R2/SigV4 max is 7 days). The 302 is cached only briefly on purpose:
# a long cache means a serve-path change (e.g. an R2 credential rotation) leaves browsers
# and the edge following a *cached* redirect to a presigned URL signed with the
# rotated-out key — broken images for days, not fixable by a client hard-reload. A short
# TTL makes such a change self-heal within the hour. (imageUrl.ts also carries a
# cache-bust token to flush already-cached redirects on demand.)
_PRESIGN_TTL = 604800
_REDIRECT_MAX_AGE = 3600  # 1 hour

_client: image_storage.R2Client | None = None


def _r2() -> image_storage.R2Client | None:
    global _client
    if _client is None and image_storage.is_configured():
        _client = image_storage.R2Client.from_env()
    return _client


@router.get("/{key:path}")
def get_image(key: str) -> RedirectResponse:
    if not _KEY_RE.match(key):
        raise HTTPException(status_code=404, detail="Not found")
    client = _r2()
    if client is None:
        raise HTTPException(status_code=503, detail="Image storage not configured")
    url = client.presigned_get(key, expires_in=_PRESIGN_TTL)
    return RedirectResponse(
        url,
        status_code=302,
        headers={"Cache-Control": f"public, max-age={_REDIRECT_MAX_AGE}"},
    )
