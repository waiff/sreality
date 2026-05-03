"""FastAPI dependencies: per-request DB connection, shared SrealityClient."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

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
