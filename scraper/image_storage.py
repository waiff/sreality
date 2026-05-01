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

LOG = logging.getLogger(__name__)

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


def download_image(url: str, timeout: float = 15.0) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


class R2Client:
    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
    ) -> None:
        self.bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    @classmethod
    def from_env(cls) -> R2Client:
        return cls(
            account_id=_required("R2_ACCOUNT_ID"),
            access_key_id=_required("R2_ACCESS_KEY_ID"),
            secret_access_key=_required("R2_SECRET_ACCESS_KEY"),
            bucket=_required("R2_BUCKET_NAME"),
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


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} environment variable is not set")
    return value
