"""Tests for /admin/condition-scoring/regions — per-kraj scoring toggles.

The /admin prefix is bearer-gated (CLAUDE.md rule #8), but these tests leave
API_TOKEN unset so the gate no-ops (the dedicated gate assertions live in
test_admin_routes.py). The fake conn serves the kraj SELECT, the app_settings
key read, the unscored GROUP BY, and the PUT's INSERT ... ON CONFLICT.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main

_KRAJE = [
    (27, "Hlavní město Praha"),
    (43, "Jihočeský kraj"),
    (86, "Moravskoslezský kraj"),
]


class _Cursor:
    def __init__(self, parent: "_Conn") -> None:
        self._p = parent
        self._last: Any = None
        self._rows: list[Any] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> None:
        s = " ".join(sql.split()).lower()
        if s.startswith("select id, name from admin_boundaries"):
            self._rows = list(_KRAJE)
            self._last = None
        elif s.startswith(
            "select key, value, description, updated_at from app_settings where key"
        ):
            key = params[0]
            row = self._p.app_settings.get(key)
            self._last = (
                (key, row["value"], row.get("description"), None)
                if row is not None else None
            )
        elif s.startswith("select region_id, count(*) from listings"):
            self._rows = list(self._p.unscored_counts.items())
            self._last = None
        elif s.startswith("insert into app_settings"):
            key, value, description, updated_by = params
            existing = self._p.app_settings.get(key)
            self._p.app_settings[key] = {
                "value": json.loads(value),
                "description": (
                    existing["description"] if existing else description
                ),
                "updated_by": updated_by,
            }
            self._last = None

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Conn:
    def __init__(
        self,
        app_settings: dict[str, dict[str, Any]],
        unscored_counts: dict[int | None, int],
    ) -> None:
        self.app_settings = app_settings
        self.unscored_counts = unscored_counts

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self):  # noqa: ANN201
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield self

        return _ctx()


def _make_client(conn: _Conn) -> Any:
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    return TestClient(api_main.app)


@pytest.fixture()
def conn():
    c = _Conn(
        app_settings={
            "condition_scoring_enabled_region_ids": {
                "value": [27, 86],
                "description": "seeded",
            },
        },
        unscored_counts={27: 120, 86: 9, None: 7},
    )
    yield c
    api_main.app.dependency_overrides.clear()


def test_get_merges_enabled_flags_and_counts(conn):
    client = _make_client(conn)
    res = client.get("/admin/condition-scoring/regions")
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    by_id = {r["id"]: r for r in data["regions"]}
    assert set(by_id) == {27, 43, 86}
    assert by_id[27]["enabled"] is True
    assert by_id[43]["enabled"] is False
    assert by_id[86]["enabled"] is True
    assert by_id[27]["unscored_active"] == 120
    assert by_id[43]["unscored_active"] == 0
    assert data["parked_no_geo"] == 7
    assert data["enabled_region_ids"] == [27, 86]


def test_get_treats_missing_key_as_empty(conn):
    conn.app_settings.clear()
    client = _make_client(conn)
    res = client.get("/admin/condition-scoring/regions")
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["enabled_region_ids"] == []
    assert all(r["enabled"] is False for r in data["regions"])


def test_put_persists_full_list(conn):
    client = _make_client(conn)
    res = client.put(
        "/admin/condition-scoring/regions",
        json={"enabled_region_ids": [43]},
    )
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["enabled_region_ids"] == [43]
    by_id = {r["id"]: r for r in data["regions"]}
    assert by_id[43]["enabled"] is True
    assert by_id[27]["enabled"] is False
    stored = conn.app_settings["condition_scoring_enabled_region_ids"]
    assert stored["value"] == [43]
    assert stored["updated_by"] == "settings_ui"


def test_put_creates_key_when_absent(conn):
    conn.app_settings.clear()
    client = _make_client(conn)
    res = client.put(
        "/admin/condition-scoring/regions",
        json={"enabled_region_ids": [27, 86]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["data"]["enabled_region_ids"] == [27, 86]
    assert (
        conn.app_settings["condition_scoring_enabled_region_ids"]["value"]
        == [27, 86]
    )


def test_put_rejects_unknown_kraj_id(conn):
    client = _make_client(conn)
    res = client.put(
        "/admin/condition-scoring/regions",
        json={"enabled_region_ids": [27, 999]},
    )
    assert res.status_code == 422
    # store untouched
    stored = conn.app_settings["condition_scoring_enabled_region_ids"]
    assert stored["value"] == [27, 86]
