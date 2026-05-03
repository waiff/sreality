"""FastAPI dependencies: per-request DB connection, shared SrealityClient, auth."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

from fastapi import Header, HTTPException

from scraper import db
from scraper.sreality_client import SrealityClient

if TYPE_CHECKING:
    import psycopg


def get_db_conn() -> "Iterator[psycopg.Connection]":
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


_CLIENT: SrealityClient | None = None


def get_sreality_client() -> SrealityClient:
    """Module-level singleton so the per-instance throttle persists across requests."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = SrealityClient()
    return _CLIENT


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate. No-op if API_TOKEN env var is unset (local dev)."""
    expected = os.environ.get("API_TOKEN")
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="Invalid or missing token")
