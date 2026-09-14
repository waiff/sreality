"""W10, against a real schema: a 410 body stops being the latest body.

The offline companion (`test_location_w10_gone_bodies.py`) pins the migration's
predicates and the three places the pass asks for a successful body. Neither can
answer the question this file exists for — what Postgres actually SELECTS once a
stored gone page is stamped — because the rule is an anti-join over
`portal_raw_payloads`, not Python.

The shape is the defect itself: an ad with a live page stored at v5 and, one
version later, the category-index page bazos answers with after the ad is removed,
stored at HTTP 200. Today the pass mines that category page and never sees the live
one. Stamp it 410 and the SAME query returns the live page instead — no filter, no
flag, no second definition of "latest".

Gated on TEST_DATABASE_URL, like the PREPARE sweep: runs in the schema-replay job
(.github/workflows/migrations.yml), skips in the normal offline suite. Everything
rolls back.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from location_data.claims_intake import _LATEST_BODY_ONLY

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — W10's live check runs only in the CI DB job",
)

SOURCE = "w10_probe"
NATIVE = "w10-1"
BASE = datetime(2026, 9, 14, 8, 0, tzinfo=UTC)

_INSERT = """
    INSERT INTO portal_raw_payloads
        (source, source_id_native, page_kind, payload_sha256, body_sha256, body,
         content_type, byte_size, http_status, contract_version,
         first_observed_at, last_observed_at, fetched_at)
    VALUES (%(source)s, %(native)s, 'detail', %(sha)s, %(sha)s, %(body)s,
            'text/html', %(size)s, %(status)s, %(version)s,
            %(at)s, %(at)s, %(at)s)
    RETURNING id
"""

# The pass's own rule, read straight off the module: the latest body of this key
# whose fetch succeeded.
_SELECT = """
    SELECT p.id FROM portal_raw_payloads p
    WHERE p.source = %(source)s
      AND p.page_kind = 'detail'
      AND (p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299)
""" + _LATEST_BODY_ONLY


def _add(cur, *, body: bytes, status: int | None, at: datetime, version: int | None) -> int:
    cur.execute(_INSERT, {
        "source": SOURCE, "native": NATIVE, "sha": body.ljust(32, b"\0")[:32],
        "body": body, "size": len(body), "status": status, "version": version, "at": at,
    })
    return int(cur.fetchone()[0])


def test_a_stamped_gone_page_hands_the_lane_back_the_live_body():
    import psycopg

    with psycopg.connect(_DB_URL) as conn:
        with conn.cursor() as cur:
            live = _add(
                cur, body=b"<html>Byt 3+1, Kolin</html>", status=200,
                at=BASE, version=5,
            )
            gone = _add(
                cur, body=b"<title>Byty inzerce - Reality | Bazos.cz</title>",
                status=200, at=BASE + timedelta(days=3), version=None,
            )

            cur.execute(_SELECT, {"source": SOURCE})
            assert [r[0] for r in cur.fetchall()] == [gone], (
                "the defect: the category page is the latest body, so it is what is mined"
            )

            cur.execute(
                "UPDATE portal_raw_payloads SET http_status = 410 WHERE id = %s", (gone,)
            )

            cur.execute(_SELECT, {"source": SOURCE})
            assert [r[0] for r in cur.fetchall()] == [live], (
                "after the stamp the last LIVE page is the latest body, and it is "
                "version-eligible (contract_version 5, never the active one)"
            )
        conn.rollback()
