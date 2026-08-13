"""The churn counters' arithmetic, executed against the replayed schema.

The whole deliverable of W2a-0 is a NUMBER the operator takes a tens-of-GB storage
decision on. The offline suite (tests/test_payload_churn_write.py) proves the hook
is invisible when off and unkillable when on, and CI's PREPARE sweep proves the
statement compiles — but neither ever runs the `ON CONFLICT` arithmetic, so a
wrong-direction comparison or an off-by-one would ship green and only surface a
week later as a nonsense readout.

This module runs it: first fetch, identical refetch, changed refetch, a replayed
batch, and a normaliser-version bump. Gated on TEST_DATABASE_URL exactly like
tests/test_sql_schema_prepare.py, so a normal local `pytest` skips it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest

from scraper import db

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — live churn arithmetic runs only in the CI DB job",
)

_JSON = "application/json"


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _row(conn: psycopg.Connection, key: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT fetches, raw_changes, norm_changes, normalizer_version, "
            "       first_seen_at, last_seen_at "
            "FROM portal_payload_churn WHERE source_id_native = %s "
            "ORDER BY normalizer_version",
            (key,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, rows
    r = rows[0]
    return {
        "fetches": r[0], "raw_changes": r[1], "norm_changes": r[2],
        "version": r[3], "first_seen_at": r[4], "last_seen_at": r[5],
    }


def _record(conn: psycopg.Connection, key: str, body: bytes, observation: str) -> None:
    db.record_payload_churn(
        conn,
        source="sreality",
        source_id_native=key,
        page_kind="detail",
        body=body,
        content_type=_JSON,
        observation=observation,
    )


def test_counters_follow_the_hashes(conn: psycopg.Connection) -> None:
    key = f"live-{uuid.uuid4().hex}"

    _record(conn, key, b'{"price": 1}', "obs-1")
    assert (_row(conn, key)["fetches"], _row(conn, key)["norm_changes"]) == (1, 0)

    # Byte-different, content-identical: JSON canonicalisation must absorb it, so
    # the raw hash moves and the normalised one does not.
    _record(conn, key, b'{"price":   1}', "obs-2")
    after = _row(conn, key)
    assert (after["fetches"], after["raw_changes"], after["norm_changes"]) == (2, 1, 0)

    # Genuinely changed body: both move.
    _record(conn, key, b'{"price": 2}', "obs-3")
    after = _row(conn, key)
    assert (after["fetches"], after["raw_changes"], after["norm_changes"]) == (3, 2, 1)

    # Identical body AND a new fetch: counted as a fetch, not as a change.
    _record(conn, key, b'{"price": 2}', "obs-4")
    after = _row(conn, key)
    assert (after["fetches"], after["raw_changes"], after["norm_changes"]) == (4, 2, 1)


def test_a_replayed_observation_bumps_nothing(conn: psycopg.Connection) -> None:
    # What _flush_drain_batch does on a transient pooler drop: re-run the whole
    # write op with the same DrainItems, i.e. the same per-fetch tokens.
    key = f"live-{uuid.uuid4().hex}"

    _record(conn, key, b'{"price": 1}', "obs-1")
    _record(conn, key, b'{"price": 2}', "obs-2")
    before = _row(conn, key)

    _record(conn, key, b'{"price": 2}', "obs-2")
    _record(conn, key, b'{"price": 2}', "obs-2")

    assert _row(conn, key) == before


def test_a_normaliser_bump_opens_a_clean_cohort(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A profile tweak mid-measurement must not relabel accumulated counters onto
    # the new version (blended cohort) nor register a phantom change on the first
    # fetch under it (the hash moved because the normaliser moved).
    from location_data import payload_norm

    shipped = payload_norm.NORMALIZER_VERSION
    key = f"live-{uuid.uuid4().hex}"
    _record(conn, key, b'{"price": 1}', "obs-1")
    _record(conn, key, b'{"price": 2}', "obs-2")

    monkeypatch.setattr(payload_norm, "NORMALIZER_VERSION", "payload_norm@test")
    _record(conn, key, b'{"price": 2}', "obs-3")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT normalizer_version, fetches, raw_changes, norm_changes "
            "FROM portal_payload_churn WHERE source_id_native = %s",
            (key,),
        )
        cohorts = {r[0]: tuple(r[1:]) for r in cur.fetchall()}

    assert cohorts == {shipped: (2, 1, 1), "payload_norm@test": (1, 0, 0)}


def test_the_hook_writes_through_the_flag(conn: psycopg.Connection) -> None:
    # End-to-end through the wrapper the portals actually call, including the
    # thunked body and the app_settings gate.
    key = f"live-{uuid.uuid4().hex}"
    setting = db.PAYLOAD_SHADOW_HASH_SETTING
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_settings WHERE key = %s", (setting,))

    db.clear_app_settings_flag_cache()
    db.record_payload_churn_if_enabled(
        conn, source="sreality", source_id_native=key, page_kind="detail",
        body=lambda: b'{"price": 1}', content_type=_JSON, observation="obs-1",
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM portal_payload_churn WHERE source_id_native = %s",
            (key,),
        )
        assert cur.fetchone()[0] == 0

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES (%s, 'true'::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (setting,),
        )
    db.clear_app_settings_flag_cache()
    try:
        db.record_payload_churn_if_enabled(
            conn, source="sreality", source_id_native=key, page_kind="detail",
            body=lambda: b'{"price": 1}', content_type=_JSON, observation="obs-1",
        )
        assert _row(conn, key)["fetches"] == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = %s", (setting,))
        db.clear_app_settings_flag_cache()
