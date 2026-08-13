"""W2a-2's dual-write executed end to end, against the replayed schema.

tests/test_payload_dual_write.py proves the wiring with a fake connection: which
fetches reach the archive, with which body, how often. A fake connection cannot
prove the two things that decide whether this is safe to switch on in production:
that the parameters the chokepoint passes actually satisfy the store's enum,
CHECK and UNIQUE constraints, and that a REPLAYED drain batch — `_flush_drain_batch`
retrying the whole write op after a transient pooler drop — collides instead of
appending a second version. Both need real SQL.

Gated on TEST_DATABASE_URL exactly like tests/test_payload_churn_live.py, so a
normal local `pytest` skips it. Nothing here touches production: rows are keyed on
a per-test uuid in a throwaway container.
"""

from __future__ import annotations

import json
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
    reason="TEST_DATABASE_URL not set — the live dual-write runs in the CI DB job",
)

_SOURCE = "idnes"
_PAGE = "<html><body><h1>Byt 3+1</h1><p>Dlouhá 1</p></body></html>"


@pytest.fixture()
def conn() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_DB_URL, autocommit=True) as c:
        yield c


def _set_dual_write(
    conn: psycopg.Connection, enabled: bool, *, index_archive: bool = False,
) -> None:
    """Flip the archive on through the GLOBAL limit layer.

    * the global layer, not `portals.operational_limits`, so the test does not
      depend on the registry carrying a row for this source in the replayed
      schema; the per-portal layer and its precedence are unit-tested in
      tests/scraper/test_portal.py;
    * `index_archive` is W2a-6's second gate, which a `page_kind='index'` write
      needs on TOP of this one — default off, so a detail-page test says nothing
      about it either way.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES "
            "('scraper_limits_global', %s::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (json.dumps({
                "payload_dual_write": enabled,
                "payload_index_archive": index_archive,
            }),),
        )
    db.clear_app_settings_flag_cache()


@pytest.fixture(autouse=True)
def _restore_limits(conn: psycopg.Connection) -> Iterator[None]:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = 'scraper_limits_global'")
        row = cur.fetchone()
    db.clear_app_settings_flag_cache()
    yield
    with conn.cursor() as cur:
        if row is None:
            cur.execute("DELETE FROM app_settings WHERE key = 'scraper_limits_global'")
        else:
            cur.execute(
                "UPDATE app_settings SET value = %s::jsonb "
                "WHERE key = 'scraper_limits_global'",
                (json.dumps(row[0]),),
            )
    db.clear_app_settings_flag_cache()


def _archive(
    conn: psycopg.Connection, key: str, html: str, *, page_kind: str = "detail",
) -> None:
    db.upsert_portal_raw_page(
        conn,
        source=_SOURCE,
        source_id_native=key,
        source_url="https://reality.idnes.cz/x",
        page_kind=page_kind,
        html=html,
        http_status=200,
    )


def _payloads(conn: psycopg.Connection, key: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_seq, page_kind::text, content_type, content_encoding, "
            "       http_status, byte_size, octet_length(body), normalizer_version, "
            "       length(payload_sha256), length(body_sha256), pinned, "
            "       first_observed_at, last_observed_at, listing_id, contract_version "
            "  FROM portal_raw_payloads WHERE source = %s AND source_id_native = %s "
            " ORDER BY version_seq",
            (_SOURCE, key),
        )
        cols = [
            "version_seq", "page_kind", "content_type", "content_encoding",
            "http_status", "byte_size", "inline_bytes", "normalizer_version",
            "sha_len", "body_sha_len", "pinned", "first_observed_at",
            "last_observed_at", "listing_id", "contract_version",
        ]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def test_gate_off_writes_the_staging_row_and_no_payload(
    conn: psycopg.Connection,
) -> None:
    key = f"live-{uuid.uuid4().hex}"
    _set_dual_write(conn, False)

    _archive(conn, key, _PAGE)

    assert _payloads(conn, key) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM portal_raw_pages "
            " WHERE source = %s AND source_id_native = %s",
            (_SOURCE, key),
        )
        assert cur.fetchone()[0] == 1


def test_gate_on_archives_one_body_the_store_accepts(conn: psycopg.Connection) -> None:
    # Every column the chokepoint fills, read back: a fake connection cannot tell
    # you that 'detail' is a legal location_page_kind or that prp_body_present held.
    key = f"live-{uuid.uuid4().hex}"
    _set_dual_write(conn, True)

    _archive(conn, key, _PAGE)

    rows = _payloads(conn, key)
    assert len(rows) == 1
    row = rows[0]
    assert row["version_seq"] == 1
    assert row["page_kind"] == "detail"
    assert row["content_type"] == "text/html"
    assert row["http_status"] == 200
    assert row["byte_size"] == len(_PAGE.encode("utf-8"))
    assert row["inline_bytes"] is not None  # R2 is unconfigured in CI: body inline
    assert row["normalizer_version"]
    assert (row["sha_len"], row["body_sha_len"]) == (32, 32)
    assert row["pinned"] is True  # first AND latest version
    assert row["first_observed_at"] == row["last_observed_at"]
    assert row["listing_id"] is None and row["contract_version"] is None


def test_a_replayed_batch_appends_no_second_version(conn: psycopg.Connection) -> None:
    # The contract `_flush_drain_batch` needs: replaying a partially-committed
    # batch must not cost a version. Content addressing is what provides it.
    key = f"live-{uuid.uuid4().hex}"
    _set_dual_write(conn, True)

    _archive(conn, key, _PAGE)
    _archive(conn, key, _PAGE)

    rows = _payloads(conn, key)
    assert len(rows) == 1
    assert rows[0]["last_observed_at"] >= rows[0]["first_observed_at"]


def test_a_changed_body_appends_a_version(conn: psycopg.Connection) -> None:
    key = f"live-{uuid.uuid4().hex}"
    _set_dual_write(conn, True)

    _archive(conn, key, _PAGE)
    _archive(conn, key, _PAGE.replace("Dlouhá 1", "Dlouhá 2"))

    assert [r["version_seq"] for r in _payloads(conn, key)] == [1, 2]


def test_an_index_page_archives_under_its_own_page_kind(
    conn: psycopg.Connection,
) -> None:
    # The index archivers ride the same chokepoint; 'index' has to be a legal
    # location_page_kind label, which only real SQL can answer. Needs BOTH gates
    # since W2a-6 — an index body passes payload_index_archive as well.
    key = f"live-{uuid.uuid4().hex}/0/2026w33"
    _set_dual_write(conn, True, index_archive=True)

    _archive(conn, key, '{"_embedded": {"estates": []}}', page_kind="index")

    rows = _payloads(conn, key)
    assert len(rows) == 1
    assert rows[0]["page_kind"] == "index"
    assert rows[0]["content_type"] == "application/json"


def test_the_index_gate_alone_holds_an_index_body_back(conn: psycopg.Connection) -> None:
    # The same write with only payload_dual_write on must reach portal_raw_pages
    # and NOT the archive — the split flag's whole point, against the real schema.
    key = f"live-{uuid.uuid4().hex}/0/2026w33"
    _set_dual_write(conn, True, index_archive=False)

    _archive(conn, key, '{"_embedded": {"estates": []}}', page_kind="index")

    assert _payloads(conn, key) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM portal_raw_pages "
            " WHERE source = %s AND source_id_native = %s",
            (_SOURCE, key),
        )
        assert cur.fetchone()[0] == 1
