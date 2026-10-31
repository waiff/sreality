"""GET /properties/{id}/price-history: the SPA property page's two reads (every ad, every ad's
price snapshots) behind require_token on the service role. Hermetic: the DB is a ScriptConn."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import property_read as pr
from tests.api._jwt import mint
from tests.api._routes import auth_bucket, flat_routes
from tests.api._script_conn import ScriptConn

_QUERIES = Path(__file__).resolve().parents[2] / "frontend/src/lib/queries.ts"
_URL = "/properties/7/price-history"
AUTH = {"Authorization": f"Bearer {mint({'sub': 'u1'})}"}
_TS = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)  # psycopg reads timestamptz aware
AD = {
    "id": 11, "property_id": 7, "source": "sreality", "source_url": "https://www.sreality.cz/x",
    "source_id_native": "1", "ad_reference": None, "is_active": True, "price_czk": 5_000_000,
    "first_seen_at": _TS, "last_seen_at": _TS,
}
SNAPSHOT = {"id": 3, "listing_id": 11, "scraped_at": _TS, "price_czk": 5_000_000}


@pytest.fixture()
def serve():
    def _serve(ads: list[dict[str, Any]], snapshots: list[dict[str, Any]] = ()) -> ScriptConn:
        conn = ScriptConn([
            (lambda sql: "property_sources_public" in sql, [tuple(a[c] for c in pr.SOURCE_COLS) for a in ads]),
            (lambda sql: "listing_snapshots_public" in sql, [tuple(s[c] for c in pr.SNAPSHOT_COLS) for s in snapshots]),
        ])
        api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
        return conn

    yield _serve
    api_main.app.dependency_overrides.clear()


def test_serves_every_ad_and_their_snapshots_whole_in_order(serve) -> None:
    conn = serve([AD, {**AD, "id": 12, "is_active": False}], [SNAPSHOT])
    res = TestClient(api_main.app).get(_URL, headers=AUTH)
    assert res.status_code == 200
    data = res.json()["data"]
    assert [a["id"] for a in data["sources"]] == [11, 12]
    # FastAPI writes an aware timestamp with `Z` (the lookup's `_clean` writes +00:00): parse, never compare.
    assert data["snapshots"] == [{**SNAPSHOT, "scraped_at": "2026-10-01T08:00:00Z"}]
    assert [params for _, params in conn.executed] == [([7],), ([11, 12],)]


def test_an_unknown_property_is_404_and_reads_no_snapshot(serve) -> None:
    conn = serve([])
    res = TestClient(api_main.app).get(_URL, headers=AUTH)
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "not_found"
    assert len(conn.executed) == 1


@pytest.mark.parametrize("pid", [2**63, -(2**63) - 1])
def test_an_id_outside_bigint_is_404_and_reads_nothing(serve, pid: int) -> None:
    """Bound as an array, it would fail the statement's ::bigint[] cast (a 500); it names nothing we hold."""
    conn = serve([AD])
    res = TestClient(api_main.app).get(f"/properties/{pid}/price-history", headers=AUTH)
    assert res.status_code == 404 and not conn.executed


def test_reads_the_spas_columns_from_the_public_views_only(serve) -> None:
    """The whole statement: the SPA's columns, one view and no join, the key, the order, no cap."""
    spa = _QUERIES.read_text(encoding="utf-8")
    ad_cols = re.search(r"PROPERTY_SOURCE_COLS =\s*'([^']+)'", spa).group(1)
    snap_cols = re.search(r"from\('listing_snapshots_public'\)\s*\.select\('([^']+)'", spa).group(1)
    conn = serve([AD], [SNAPSHOT])
    TestClient(api_main.app).get(_URL, headers=AUTH)
    for (sql, _), cols, rest in zip(conn.executed, (ad_cols, snap_cols), (
        ("property_sources_public", "property_id = any(%s::bigint[])", "first_seen_at, id"),
        ("listing_snapshots_public", "listing_id = any(%s::bigint[])", "scraped_at, id"),
    ), strict=True):
        shape = re.fullmatch(r"select (.+?) from (\w+) where (.+?) order by (.+)", sql)
        assert shape, sql
        assert shape.group(1).replace(" ", "") == cols
        assert shape.groups()[1:] == rest


def test_requires_a_token_and_never_the_tenant_connection(serve, monkeypatch) -> None:
    serve([AD], [SNAPSHOT])
    monkeypatch.delenv("API_AUTH_OPTIONAL", raising=False)  # tests/conftest opens it for dev
    client = TestClient(api_main.app)
    assert client.get(_URL, headers=AUTH).status_code == 200
    assert client.get(_URL).status_code == 401
    (route,) = [r for r in flat_routes() if r.path.endswith("/price-history")]
    assert auth_bucket(route) == "token"  # require_token; no tenant_conn, no verify_jwt
