"""Download Sreality images and upload them to Cloudflare R2.

Optional: callers should check is_configured() before calling R2Client
methods. Without R2_* env vars the image-download phase is a no-op.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import boto3
import requests
from botocore.config import Config as BotoConfig

from scraper import media

LOG = logging.getLogger(__name__)


class NotAnImageError(Exception):
    """Raised when a download is too large or its bytes are not a known image.

    Terminal (not transient): the image-download loop routes it to
    `mark_image_unavailable(reason='not_an_image')` so the row leaves the queue
    and never trips the suspicious-stop circuit-breaker.
    """

R2_ENV_VARS: tuple[str, ...] = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET_NAME",
)


def is_configured() -> bool:
    return all(os.environ.get(v) for v in R2_ENV_VARS)


def image_key(sreality_id: int, sequence: int | None) -> str:
    """Bucket key for one image. Sequence padded to 4 digits for stable sort."""
    seq = sequence if sequence is not None else 0
    return f"{sreality_id}/{seq:04d}.jpg"


# sreality's v1 rebuild serves bare image URLs; the CDN 401s a bare URL and
# only returns bytes when the render-transform query is present. Pre-rebuild
# stored URLs already carry the complete chain.
IMAGE_TRANSFORM_OPS = "res,749,562,3|shr,,20|jpg,90"
IMAGE_TRANSFORM = "fl=" + IMAGE_TRANSFORM_OPS

# The render-transform is a sreality CDN (*.sdn.cz / Seznam) feature. Other
# portals (bazos and onward) serve plain image URLs and would 404/ignore the
# query, so the transform is gated on the sreality host — keeping download_image
# portal-agnostic now that non-sreality images flow through it (multi-portal).
_SREALITY_IMAGE_HOST = "sdn.cz"


def _with_transform(url: str) -> str:
    if _SREALITY_IMAGE_HOST not in url:
        return url
    if "fl=" not in url:
        return f"{url}{'&' if '?' in url else '?'}{IMAGE_TRANSFORM}"
    if "res," in url:
        # Legacy stored URL with a complete chain — already renderable.
        return url
    # Prefix chain like '?fl=rot,180,0|' (trailing pipe): the CDN 400s it as-is
    # AND with the pipe stripped; only completing the chain returns bytes. The
    # rot op MUST be preserved — completing without it returns 200 but stores
    # the photo unrotated (curl-verified).
    return url.rstrip("|") + "|" + IMAGE_TRANSFORM_OPS


def download_image(url: str, timeout: float = 15.0) -> bytes:
    """Download one image, capped at media.MAX_IMAGE_BYTES.

    Streams so an oversize body (e.g. a video served under an image-looking URL)
    is rejected without buffering it all into memory — a Content-Length over the
    cap short-circuits before the first byte. Raises NotAnImageError on oversize.
    """
    with requests.get(_with_transform(url), timeout=timeout, stream=True) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > media.MAX_IMAGE_BYTES:
            raise NotAnImageError(
                f"declared size {declared} exceeds {media.MAX_IMAGE_BYTES} bytes"
            )
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            buf += chunk
            if len(buf) > media.MAX_IMAGE_BYTES:
                raise NotAnImageError(
                    f"body exceeds {media.MAX_IMAGE_BYTES} bytes"
                )
        return bytes(buf)


class R2Client:
    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        max_pool_connections: int = 32,
    ) -> None:
        self.bucket = bucket
        # Pool sized to the download worker count — the default of 10 caused
        # constant "Connection pool is full, discarding connection" churn
        # under the 32-worker image phase.
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
            config=BotoConfig(max_pool_connections=max(10, max_pool_connections)),
        )

    @classmethod
    def from_env(cls, max_pool_connections: int = 32) -> R2Client:
        return cls(
            account_id=_required("R2_ACCOUNT_ID"),
            access_key_id=_required("R2_ACCESS_KEY_ID"),
            secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
            bucket=_required("R2_BUCKET_NAME"),
            max_pool_connections=max_pool_connections,
        )

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = "image/jpeg",
    ) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            CacheControl="public, max-age=2592000",
        )

    def download_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def presigned_get(self, key: str, expires_in: int = 604800) -> str:
        """Time-limited GET URL for one object, so a private bucket can still
        serve image bytes straight to the browser (no proxying through us)."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value
