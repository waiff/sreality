"""Download Sreality images and upload them to Cloudflare R2.

Optional: callers should check is_configured() before calling R2Client
methods. Without R2_* env vars the image-download phase is a no-op.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Any
from urllib.parse import quote

import requests

from scraper import media

LOG = logging.getLogger(__name__)

# SigV4's `X-Amz-Date` wire format.
_SIGV4_TIMESTAMP = "%Y%m%dT%H%M%SZ"

_ANCHORED_AUTH_CLS: Any = None


def presign_anchor_stamp(anchor_seconds: int, now: float | None = None) -> str:
    """`X-Amz-Date` for the start of the bucket of width `anchor_seconds` containing `now`."""
    epoch = int(now if now is not None else time.time())
    start = epoch - (epoch % anchor_seconds)
    return datetime.datetime.fromtimestamp(start, datetime.timezone.utc).strftime(
        _SIGV4_TIMESTAMP
    )


def _anchored_auth_cls() -> Any:
    """Built on first use so importing this module still costs no botocore (see R2Client)."""
    global _ANCHORED_AUTH_CLS
    if _ANCHORED_AUTH_CLS is None:
        from botocore.auth import S3SigV4QueryAuth

        class _AnchoredSigV4QueryAuth(S3SigV4QueryAuth):  # type: ignore[misc]
            """SigV4 query signer with the signing time pinned instead of read off the clock.

            `add_auth` stamps `request.context['timestamp']` with "now" and then calls
            `_modify_request_before_signing`; every consumer downstream of that — the
            `X-Amz-Date` param, the credential scope, the string-to-sign and the derived
            signing key — reads it back out of the context. Overwriting it here, before
            `super()` builds the auth params, therefore pins all four consistently without
            reimplementing any of botocore's signing.

            The base class is the S3 one (`s3v4-query`, what boto3 itself resolves for a
            presigned `get_object`) and not the generic `SigV4QueryAuth`: S3 signs the
            constant `UNSIGNED-PAYLOAD` rather than a body hash and skips path
            normalisation. With the generic base every field of the URL matches and only
            the signature is wrong — i.e. it fails at R2, not here.
            """

            def __init__(
                self, credentials: Any, region_name: str, expires: int, timestamp: str
            ) -> None:
                super().__init__(credentials, "s3", region_name, expires=expires)
                self._pinned_timestamp = timestamp

            def _modify_request_before_signing(self, request: Any) -> None:
                request.context["timestamp"] = self._pinned_timestamp
                super()._modify_request_before_signing(request)

        _ANCHORED_AUTH_CLS = _AnchoredSigV4QueryAuth
    return _ANCHORED_AUTH_CLS


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


def image_key(listing_id: int, image_id: int) -> str:
    """Bucket key for one image — unique per ROW by construction.

    Both earlier schemes keyed the prefix on a value that is not unique across
    rows and drew it from ONE numeric namespace: `{sreality_id}/{seq:04d}` (pre
    Gate-2) then `{listings.id}/{seq:04d}`. So a NEW listing whose surrogate id
    happened to equal an OLDER listing's sreality_id minted the SAME key, and the
    second upload silently overwrote the first — leaving the older row serving
    another listing's photo while still carrying the pHash of the bytes it lost
    (16 objects / 32 rows; repaired by migration 371). `images.id` is the primary
    key and the `img/` namespace cannot collide with a bare-numeric prefix, so
    neither half of that can recur. Only NEWLY stored images take this scheme —
    nothing recomputes a key for an existing row, so the bucket holds all three.
    """
    return f"img/{listing_id}/{image_id}.jpg"


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
        # boto3/botocore are imported here, not at module top, so that
        # importing this module (and anything that transitively pulls it in —
        # notably the toolkit package) costs nothing unless R2 is actually used.
        # R2 is optional (see module docstring + is_configured()); a client is
        # only ever built when R2_* env vars are present.
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        # Kept for the anchored presign below, which signs through botocore's auth
        # classes directly rather than through the client's private request signer.
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
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

    def upload_file(self, key: str, path: str, content_type: str = "application/zip") -> None:
        """Stream a file from disk (boto3 chunks + multiparts it) — a 253 MB registry
        artefact must never be read into memory the way upload_bytes would."""
        with open(path, "rb") as handle:
            self._client.upload_fileobj(
                handle, self.bucket, key, ExtraArgs={"ContentType": content_type}
            )

    def object_size(self, key: str) -> int | None:
        """Size in bytes, or None ONLY when the object does not exist."""
        from botocore.exceptions import ClientError

        try:
            return int(self._client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            code = str((exc.response.get("Error") or {}).get("Code"))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def presigned_get(
        self, key: str, expires_in: int = 604800, anchor_seconds: int = 0
    ) -> str:
        """Time-limited GET URL for one object, so a private bucket can still
        serve image bytes straight to the browser (no proxying through us).

        `anchor_seconds` > 0 signs as at the START of the current bucket of that width
        instead of at "now", so every re-mint inside one bucket returns a byte-identical
        string. The browser's HTTP cache keys on the whole URL including the signature,
        so a fresh signature per request makes already-downloaded bytes unaddressable and
        re-downloads them; a stable one lets the object's own `max-age` do its job. The
        URL still expires `expires_in` after the anchor, so the anchor must stay well
        below it — the remainder is how long the last URL of a bucket keeps working.
        """
        if anchor_seconds <= 0:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=expires_in,
            )

        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        request = AWSRequest(
            method="GET",
            url=f"{self._client.meta.endpoint_url}/{self.bucket}/{quote(key, safe='/~')}",
        )
        _anchored_auth_cls()(
            Credentials(self._access_key_id, self._secret_access_key),
            self._client.meta.region_name,
            expires_in,
            presign_anchor_stamp(anchor_seconds),
        ).add_auth(request)
        return request.url


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value
