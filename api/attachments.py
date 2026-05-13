"""Operator-supplied attachments for building_runs.

Wraps scraper.image_storage.R2Client for the upload / list / delete /
read paths reachable through /buildings/{id}/attachments/* and the
read_floor_plan toolkit function.

Bucket key prefix `custom-attachments/building/{building_run_id}/{uuid}.{ext}`.
Distinct from the scraper's `{sreality_id}/{seq:04d}.jpg` keys so a
listing's images and a building_run's operator uploads never collide
on prefix-listing operations.

Unlike the scraper image-download phase (which silently no-ops when
R2 is not configured), upload here HARD-FAILS with HTTP 503 — silent
fallback would let the operator believe they attached a file the
agent never sees.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, UploadFile

from scraper import image_storage

if TYPE_CHECKING:
    import psycopg

LOG = logging.getLogger(__name__)


ALLOWED_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

MAX_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_RUN = 20


def _require_r2() -> image_storage.R2Client:
    if not image_storage.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "R2 storage is not configured on this deployment; cannot "
                "accept attachments. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME."
            ),
        )
    return image_storage.R2Client.from_env()


def _storage_key(building_run_id: int, mime_type: str) -> str:
    ext = ALLOWED_MIME[mime_type]
    return f"custom-attachments/building/{building_run_id}/{uuid.uuid4().hex}{ext}"


def insert_attachment(
    conn: "psycopg.Connection",
    *,
    building_run_id: int,
    file: UploadFile,
    uploaded_by: str | None = None,
) -> dict[str, Any]:
    """Upload one image to R2 and persist its metadata row.

    Returns the inserted attachment row as a dict. Caller is expected to
    have already validated that the building_run_id exists and is in an
    editable status — this function is the storage primitive, not the
    status-gate.
    """
    mime = (file.content_type or "").lower()
    if mime not in ALLOWED_MIME:
        raise HTTPException(
            status_code=415,
            detail=(
                f"unsupported attachment mime_type={mime!r}; allowed: "
                + ", ".join(sorted(ALLOWED_MIME))
            ),
        )

    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    if len(data) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"attachment is {len(data)} bytes; max is {MAX_BYTES}",
        )

    sha256 = hashlib.sha256(data).hexdigest()
    width_px, height_px = _probe_dimensions(data, mime)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM building_run_attachments WHERE building_run_id = %s",
            (building_run_id,),
        )
        (existing,) = cur.fetchone()
        if existing >= MAX_FILES_PER_RUN:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"building_run has {existing} attachments; cap is "
                    f"{MAX_FILES_PER_RUN}. Delete some before uploading more."
                ),
            )
        cur.execute(
            "SELECT id FROM building_run_attachments "
            "WHERE building_run_id = %s AND sha256_hex = %s",
            (building_run_id, sha256),
        )
        if cur.fetchone() is not None:
            raise HTTPException(
                status_code=409,
                detail="attachment with identical content already exists on this building_run",
            )

    r2 = _require_r2()
    key = _storage_key(building_run_id, mime)
    r2.upload_bytes(key, data, content_type=mime)

    filename = file.filename or "untitled"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO building_run_attachments (
                building_run_id, storage_key, filename, mime_type,
                byte_size, width_px, height_px, sha256_hex, uploaded_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, building_run_id, storage_key, filename, mime_type,
                      byte_size, width_px, height_px, sha256_hex, uploaded_by,
                      created_at
            """,
            (
                building_run_id, key, filename, mime, len(data),
                width_px, height_px, sha256, uploaded_by,
            ),
        )
        row = cur.fetchone()
        conn.commit()

    return _row_to_dict(row)


def list_attachments(
    conn: "psycopg.Connection",
    building_run_id: int,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, building_run_id, storage_key, filename, mime_type,
                   byte_size, width_px, height_px, sha256_hex, uploaded_by,
                   created_at
            FROM building_run_attachments
            WHERE building_run_id = %s
            ORDER BY created_at, id
            """,
            (building_run_id,),
        )
        return [_row_to_dict(r) for r in cur.fetchall()]


def fetch_attachment(
    conn: "psycopg.Connection",
    attachment_id: int,
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, building_run_id, storage_key, filename, mime_type,
                   byte_size, width_px, height_px, sha256_hex, uploaded_by,
                   created_at
            FROM building_run_attachments
            WHERE id = %s
            """,
            (attachment_id,),
        )
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


def delete_attachment(
    conn: "psycopg.Connection",
    building_run_id: int,
    attachment_id: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT storage_key FROM building_run_attachments "
            "WHERE id = %s AND building_run_id = %s",
            (attachment_id, building_run_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        cur.execute(
            "DELETE FROM building_run_attachments WHERE id = %s",
            (attachment_id,),
        )
        conn.commit()
    # R2 object is left in place. Storage is cheap and the audit row is
    # already gone; periodic cleanup is a separate concern.


def download_attachment_bytes(
    conn: "psycopg.Connection",
    attachment_id: int,
) -> tuple[bytes, str, str]:
    """Returns (bytes, mime_type, filename). Used by read_floor_plan and the
    bearer-gated thumbnail proxy."""
    row = fetch_attachment(conn, attachment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    r2 = _require_r2()
    data = r2.download_bytes(row["storage_key"])
    return data, row["mime_type"], row["filename"]


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row[0],
        "building_run_id": row[1],
        "storage_key": row[2],
        "filename": row[3],
        "mime_type": row[4],
        "byte_size": row[5],
        "width_px": row[6],
        "height_px": row[7],
        "sha256_hex": row[8],
        "uploaded_by": row[9],
        "created_at": row[10].isoformat() if row[10] is not None else None,
    }


def _probe_dimensions(data: bytes, mime: str) -> tuple[int | None, int | None]:
    """Best-effort width/height. Stdlib-only — PNG and JPEG header sniffing.

    Returns (None, None) for WebP or anything we can't sniff. Pillow
    would be cleaner but is not in the dependency list; recording
    width/height is informational so a miss is acceptable.
    """
    try:
        if mime == "image/png" and len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return width, height
        if mime == "image/jpeg":
            return _jpeg_dimensions(data)
    except Exception:  # noqa: BLE001
        LOG.debug("attachment dimension probe failed", exc_info=True)
    return None, None


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    i = 0
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None, None
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            return None, None
        marker = data[i + 1]
        if marker == 0xD8 or marker == 0xD9:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        i += 2 + seg_len
    return None, None
