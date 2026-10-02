"""Shared setup for the live property-identity tests: a rollback-always connection, the
`REQUIRED_DB` guard, and seed helpers for properties, adverts, accounts and the operator's
property-anchored state. Every seed lands inside the test's transaction and rolls back."""

from __future__ import annotations

import itertools
import os
import uuid
from typing import Any, Iterator

import pytest

DB_URL = os.environ.get("TEST_DATABASE_URL")
_REQUIRED = os.environ.get("DB_RAILS_REQUIRED") == "1"

# The migrations lane sets DB_RAILS_REQUIRED=1, so a lane that loses its URL runs the tests
# and `db_url()` fails them instead of reporting a green skip.
REQUIRED_DB = pytest.mark.skipif(
    not DB_URL and not _REQUIRED,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

OP = "ci-operator@replay.local"
_SREALITY_IDS = itertools.count(9_650_000_001)


def db_url() -> str:
    if not DB_URL:
        pytest.fail(
            "DB_RAILS_REQUIRED=1 but TEST_DATABASE_URL is not set — the migrations lane is "
            "misconfigured and these tests would otherwise have skipped green."
        )
    return DB_URL


@pytest.fixture()
def cur() -> Iterator[Any]:
    import psycopg

    conn = psycopg.connect(
        db_url(),
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
    )
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


def _property(cur: Any) -> int:
    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    return int(cur.fetchone()[0])


def _advert(cur: Any, pid: int, *, source: str, price: int = 5_000_000, active: bool = True,
            condition: str | None = None, levels: tuple[Any, Any] = (None, None)) -> int:
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, is_active, property_id, condition, "
        "building_condition_level, apartment_condition_level) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', %s, 70, %s, %s, %s, %s, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"lp-{uuid.uuid4()}", price, active, pid, condition, *levels),
    )
    return int(cur.fetchone()[0])


def _snapshot(cur: Any, listing_id: int, price: int | None, hours_ago: float) -> int:
    cur.execute(
        "INSERT INTO listing_snapshots (listing_id, scraped_at, price_czk, content_hash, raw_json) "
        "VALUES (%s, now() - make_interval(secs => %s), %s, %s, '{}'::jsonb) RETURNING id",
        (listing_id, hours_ago * 3600, price, f"lp-{uuid.uuid4()}"),
    )
    return int(cur.fetchone()[0])


def _recompute(cur: Any, pid: int) -> None:
    from scripts.recompute_property_stats import _RECOMPUTE_ONE_SQL

    cur.execute(_RECOMPUTE_ONE_SQL, {"pid": pid})


def _account(cur: Any) -> uuid.UUID:
    """A bare tenant: no auth user, no membership, no seeded stages or collections."""
    cur.execute(
        "INSERT INTO accounts (kind, name) VALUES ('personal', %s) RETURNING id",
        (f"lp-{uuid.uuid4().hex[:8]}",),
    )
    return cur.fetchone()[0]


def _stage(cur: Any, acc: uuid.UUID, *, position: int, terminal: bool = False) -> int:
    cur.execute(
        "INSERT INTO pipeline_stages (account_id, key, label, position, is_terminal) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (acc, f"lp-{uuid.uuid4().hex[:12]}", f"lp {position}", position, terminal),
    )
    return int(cur.fetchone()[0])


def _collection(cur: Any, acc: uuid.UUID) -> int:
    cur.execute(
        "INSERT INTO collections (account_id, name) VALUES (%s, %s) RETURNING id",
        (acc, f"lp-{uuid.uuid4().hex[:12]}"),
    )
    return int(cur.fetchone()[0])


def _tag(cur: Any, acc: uuid.UUID) -> int:
    # `color` is a named-palette CHECK, not a hex string.
    cur.execute(
        "INSERT INTO tags (account_id, name, color) VALUES (%s, %s, 'slate') RETURNING id",
        (acc, f"lp-{uuid.uuid4().hex[:12]}"),
    )
    return int(cur.fetchone()[0])
