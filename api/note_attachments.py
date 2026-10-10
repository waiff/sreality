"""Files attached to an operator note (migration 592): upload, read back, remove.

Every call runs on the caller's tenant connection, so RLS scopes the note and its files; a row's
account comes from its note by trigger, so no route names one. The bytes go to R2 under the
private `custom-attachments/` prefix and come back through the API, because the bucket is private
and sends no CORS header. R2 objects are never deleted: a split's note copy shares them.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import HTTPException, UploadFile

from scraper import image_storage
from toolkit.property_identity import resolve_active_property_id

if TYPE_CHECKING:
    import psycopg

# MIME type -> the extension its R2 key carries. Nothing active (HTML, SVG, script): the SPA turns
# these bytes into same-origin blob URLs. Mirrored for the file picker in
# frontend/src/lib/noteAttachments.ts (tests/test_note_attachment_rules_parity.py).
ALLOWED: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/zip": ".zip",
}
BY_EXTENSION: dict[str, str] = {ext: mime for mime, ext in ALLOWED.items()} | {
    ".jpeg": "image/jpeg",
}
MAX_BYTES = 25 * 1024 * 1024
MAX_FILES_PER_NOTE = 20

_PROJECTION = "id, note_id, filename, mime_type, byte_size, created_at"

_client: image_storage.R2Client | None = None


def _storage() -> image_storage.R2Client:
    global _client
    if not image_storage.is_configured():
        raise HTTPException(503, "attachment storage is not configured")
    if _client is None:
        _client = image_storage.R2Client.from_env()
    return _client


def clean_filename(raw: str | None) -> str:
    """The name as shown and offered on download: no path, no control characters, ≤255 chars."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        ch for ch in unicodedata.normalize("NFC", name) if unicodedata.category(ch)[0] != "C"
    ).strip()
    if len(name) > 255:
        stem, ext = os.path.splitext(name)
        name = stem[: 255 - len(ext)] + ext if len(ext) < 255 else name[:255]
    return name or "attachment"


def resolve_mime(filename: str, declared: str | None) -> str | None:
    """A named extension decides, allowed or not; only a file without one falls back to the type
    the browser declared (which is itself guessed from the name, and often empty)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext:
        return BY_EXTENSION.get(ext)
    mime = (declared or "").split(";")[0].strip().lower()
    return mime if mime in ALLOWED else None


def content_disposition(filename: str) -> str:
    fallback = filename.encode("ascii", "ignore").decode().replace('"', "").replace("\\", "")
    return f"attachment; filename=\"{fallback or 'attachment'}\"; filename*=UTF-8''{quote(filename)}"


def attachments_by_note(
    conn: "psycopg.Connection", note_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in note_ids}
    if not note_ids:
        return out
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_PROJECTION} FROM property_note_attachments "
            "WHERE note_id = ANY(%s) ORDER BY created_at, id",
            (note_ids,),
        )
        for row in cur.fetchall():
            out[int(row[1])].append(_to_attachment(row))
    return out


def add_attachment(
    conn: "psycopg.Connection", property_id: int, note_id: int, file: UploadFile,
) -> dict[str, Any]:
    data = file.file.read(MAX_BYTES + 1)
    if not data:
        raise HTTPException(400, "the file is empty")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"the file is over {MAX_BYTES // (1024 * 1024)} MB")
    filename = clean_filename(file.filename)
    mime = resolve_mime(filename, file.content_type)
    if mime is None:
        raise HTTPException(415, f"this file type cannot be attached: {filename}")
    digest = hashlib.sha256(data).hexdigest()
    property_id = resolve_active_property_id(conn, property_id) or property_id
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "SELECT count(f.id), bool_or(f.sha256_hex = %s) FROM property_notes n "
            "LEFT JOIN property_note_attachments f ON f.note_id = n.id "
            "WHERE n.id = %s AND n.property_id = %s GROUP BY n.id",
            (digest, note_id, property_id),
        )
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "note not found")
        if row[1]:
            raise HTTPException(409, f"{filename} is already attached to this note")
        if row[0] >= MAX_FILES_PER_NOTE:
            raise HTTPException(409, f"a note holds at most {MAX_FILES_PER_NOTE} files")
        key = f"custom-attachments/note/{note_id}/{uuid.uuid4().hex}{ALLOWED[mime]}"
        _storage().upload_bytes(key, data, mime)
        cur.execute(
            "INSERT INTO property_note_attachments "
            "  (note_id, storage_key, filename, mime_type, byte_size, sha256_hex) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {_PROJECTION}",
            (note_id, key, filename, mime, len(data), digest),
        )
        created = cur.fetchone()
    assert created is not None
    return _to_attachment(created)


def read_attachment(
    conn: "psycopg.Connection", property_id: int, note_id: int, attachment_id: int,
) -> tuple[bytes, str, str]:
    """(bytes, mime type, file name) of one attachment of one of the property's notes."""
    property_id = resolve_active_property_id(conn, property_id) or property_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT f.storage_key, f.mime_type, f.filename FROM property_note_attachments f "
            "JOIN property_notes n ON n.id = f.note_id "
            "WHERE f.id = %s AND f.note_id = %s AND n.property_id = %s",
            (attachment_id, note_id, property_id),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "attachment not found")
    return _storage().download_bytes(row[0]), row[1], row[2]


def delete_attachment(
    conn: "psycopg.Connection", property_id: int, note_id: int, attachment_id: int,
) -> dict[str, Any]:
    """Removes the row; the R2 object stays (a split's note copy may share it)."""
    property_id = resolve_active_property_id(conn, property_id) or property_id
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM property_note_attachments f USING property_notes n "
            "WHERE f.id = %s AND f.note_id = %s AND n.id = f.note_id AND n.property_id = %s",
            (attachment_id, note_id, property_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "attachment not found")
    return {"deleted": True}


def _to_attachment(row: tuple[Any, ...]) -> dict[str, Any]:
    created = row[5]
    return {
        "id": int(row[0]),
        "note_id": int(row[1]),
        "filename": row[2],
        "mime_type": row[3],
        "byte_size": int(row[4]),
        "created_at": created.isoformat() if isinstance(created, datetime) else created,
    }
