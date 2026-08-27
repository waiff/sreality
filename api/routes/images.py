"""Public image-redirect route: GET /images/{key} -> 302 to a presigned R2 URL.

The frontend serves every listing photo through this endpoint instead of the
sreality CDN, whose tokenised URLs expire within weeks (any stored *.sdn.cz URL
404s once sreality rotates the render token). R2 holds the durable copy; this
route mints a short-lived presigned GET so a *private* bucket can stream bytes
straight to the browser without proxying them through us.

Unauthenticated (like /health) — listing photos are public data and an <img>
tag can't send a bearer header. The key is constrained to the listing-image
shapes (`img/<listing_id>/<image_id>.jpg` and the legacy `-?<id>/<seq>.jpg`) so
this can never presign the operator-private `custom-attachments/` building
uploads that share the bucket.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from scraper import image_storage

router = APIRouter(prefix="/images", tags=["images"])

# Both listing-image shapes: the current `img/{listing_id}/{image_id}.jpg` and the
# two legacy `{native_id}/{seq:04d}.jpg` ones (native_id is negative for non-sreality
# portals). Anchored and enumerated — never a general prefix — so this still cannot
# presign the operator-private `custom-attachments/` uploads sharing the bucket.
# `[0-9]` not `\d` (which is unicode-aware, so `\d` would admit non-ASCII digits into
# a security predicate) and `\Z` not `$` (which also matches before a trailing newline).
_KEY_RE = re.compile(r"^(?:-?[0-9]+/[0-9]{4}|img/[0-9]+/[0-9]+)\.jpg\Z")

# Presign lifetime (R2/SigV4 max is 7 days). The 302 is cached only briefly on purpose:
# a long cache means a serve-path change (e.g. an R2 credential rotation) leaves browsers
# and the edge following a *cached* redirect to a presigned URL signed with the
# rotated-out key — broken images for days, not fixable by a client hard-reload. A short
# TTL makes such a change self-heal within the hour. (imageUrl.ts also carries a
# cache-bust token to flush already-cached redirects on demand.)
_PRESIGN_TTL = 604800
_REDIRECT_MAX_AGE = 3600  # 1 hour

# The redirect above re-mints hourly BY DESIGN, but SigV4 signs with the wall clock, so
# each re-mint used to hand back a different URL for the same bytes. The browser's HTTP
# cache keys on the full URL, so the object's own `Cache-Control: public, max-age=2592000`
# never applied twice: every photo re-downloaded once an hour, forever. Signing as at the
# start of the current UTC day makes the target byte-identical all day, so the hourly
# re-mint costs one header-sized 302 and zero image bytes — while the 1-hour redirect cache,
# and with it the credential-rotation self-heal above, is untouched. Must stay well under
# _PRESIGN_TTL: the last URL minted in a bucket still has TTL - anchor left to live
# (6 days here). Set IMAGE_PRESIGN_ANCHOR_SECONDS=0 to fall back to per-request signing.
_PRESIGN_ANCHOR_DEFAULT = 86400  # 1 day

_client: image_storage.R2Client | None = None


def _anchor_seconds() -> int:
    """Read per request so the kill switch takes effect without a redeploy."""
    raw = os.environ.get("IMAGE_PRESIGN_ANCHOR_SECONDS")
    if raw is None or not raw.strip().lstrip("-").isdigit():
        return _PRESIGN_ANCHOR_DEFAULT
    return min(int(raw), _PRESIGN_TTL // 2)


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
    url = client.presigned_get(
        key, expires_in=_PRESIGN_TTL, anchor_seconds=_anchor_seconds()
    )
    return RedirectResponse(
        url,
        status_code=302,
        headers={"Cache-Control": f"public, max-age={_REDIRECT_MAX_AGE}"},
    )
