"""Tests for /new-dedup/settings/* — admin CRUD over the dedup_sim_settings
registry. Admin-gated (require_admin); happy-path tests override it."""

from __future__ import annotations

import json
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, Any]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        if s.startswith("SELECT key, value FROM dedup_sim.settings"):
            self._rows = list(self._conn.table.items())
        elif s.startswith("INSERT INTO dedup_sim.settings"):
            key, value_json, _updated_by = params
            self._conn.table[key] = json.loads(value_json)
        elif s.startswith("DELETE FROM dedup_sim.settings"):
            (key,) = params
            self._conn.table.pop(key, None)

    def fetchall(self) -> list[tuple[Any, Any]]:
        return self._rows


class _Txn:
    def __enter__(self) -> "_Txn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.table: dict[str, Any] = {}

    def cursor(self) -> _Cur:
        return _Cur(self)

    def transaction(self) -> _Txn:
        return _Txn()


@pytest.fixture()
def fake_conn() -> _FakeConn:
    return _FakeConn()


@pytest.fixture()
def client(fake_conn: _FakeConn):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: fake_conn
    api_main.app.dependency_overrides[deps.require_admin] = (
        lambda: {"is_admin": True}
    )
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_list_settings_covers_full_registry(client):
    from toolkit import dedup_sim_settings as dss

    res = client.get("/new-dedup/settings")
    assert res.status_code == 200
    data = res.json()["data"]
    assert {row["key"] for row in data} == set(dss.REGISTRY)
    for row in data:
        assert row["is_override"] is False
        assert row["value"] == row["default"]


def test_put_setting_persists_and_marks_override(client):
    res = client.put("/new-dedup/settings/l2_phash_hamming_threshold", json={"value": 8})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["value"] == 8
    assert body["is_override"] is True

    follow = client.get("/new-dedup/settings").json()["data"]
    row = next(r for r in follow if r["key"] == "l2_phash_hamming_threshold")
    assert row["value"] == 8
    assert row["is_override"] is True


def test_put_setting_rejects_out_of_range_value(client):
    res = client.put("/new-dedup/settings/l2_phash_hamming_threshold", json={"value": 999})
    assert res.status_code == 400


def test_put_setting_unknown_key_404s(client):
    res = client.put("/new-dedup/settings/not_a_real_setting", json={"value": 1})
    assert res.status_code == 404


def test_delete_setting_reverts_to_default(client):
    client.put("/new-dedup/settings/l0_floor_tolerance", json={"value": 3})
    assert client.get("/new-dedup/settings").json()["data"]

    res = client.delete("/new-dedup/settings/l0_floor_tolerance")
    assert res.status_code == 200
    body = res.json()
    assert body["is_override"] is False
    assert body["value"] == body["default"] == 2


def test_delete_setting_unknown_key_404s(client):
    res = client.delete("/new-dedup/settings/not_a_real_setting")
    assert res.status_code == 404


def test_new_dedup_settings_require_admin(client):
    api_main.app.dependency_overrides.pop(deps.require_admin, None)
    assert client.get("/new-dedup/settings").status_code == 401
