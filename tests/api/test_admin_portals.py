"""Tests for /admin/portals — per-portal operational limits (migration 114).

The /admin prefix is bearer-gated (CLAUDE.md rule #8), but these tests leave
API_TOKEN unset so the gate no-ops (the dedicated gate assertions live in
test_admin_routes.py). We monkeypatch the config readers (load_portal_config /
default_config) so the fake conn only has to serve the portal-list SELECT and
the PUT select/update.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from scraper.portal import PortalLimits


def _seed_portals() -> dict[str, dict[str, Any]]:
    return {
        "sreality": {
            "label": "Sreality", "kind": "scraper",
            "sort_order": 10, "is_enabled": True, "supports_complete_walk": True,
            "operational_limits": {"detail_workers": 8, "detail_rate": 6.0},
        },
        "remax": {
            "label": "RE/MAX", "kind": "parser",
            "sort_order": 50, "is_enabled": True, "supports_complete_walk": False,
            "operational_limits": None,
        },
    }


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
        if s.startswith("select source, label, kind, sort_order") and "from portals order by" in s:
            self._rows = [
                (src, p["label"], p["kind"], p["sort_order"],
                 p["is_enabled"], p["supports_complete_walk"], p["operational_limits"])
                for src, p in self._p.portals.items()
            ]
            self._last = None
        elif s.startswith("select operational_limits from portals where source"):
            src = params[0]
            self._last = (self._p.portals[src]["operational_limits"],) if src in self._p.portals else None
        elif s.startswith("update portals set operational_limits"):
            value, _by, src = json.loads(params[0]), params[1], params[2]
            self._p.portals[src]["operational_limits"] = value
            self._last = None

    def fetchone(self) -> Any:
        return self._last

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Conn:
    def __init__(self, portals: dict[str, dict[str, Any]]) -> None:
        self.portals = portals

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self):  # noqa: ANN201
        from contextlib import contextmanager

        @contextmanager
        def _ctx():
            yield self

        return _ctx()


@pytest.fixture()
def client(monkeypatch):
    from api import routes
    admin = routes.admin

    monkeypatch.setattr(
        admin, "load_portal_config",
        lambda conn, source: SimpleNamespace(
            limits=PortalLimits(detail_workers=8, detail_rate=6.0)
        ),
    )

    def fake_default_config(source: str) -> Any:
        if source in ("remax", "idnes_reality"):
            raise ValueError("no portal config")
        return SimpleNamespace(limits=PortalLimits())

    monkeypatch.setattr(admin, "default_config", fake_default_config)

    conn = _Conn(_seed_portals())
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: conn
    c = TestClient(api_main.app)
    c._conn = conn  # type: ignore[attr-defined]
    yield c
    api_main.app.dependency_overrides.clear()


def test_get_portals_lists_all_with_effective_and_baked(client):
    res = client.get("/admin/portals")
    assert res.status_code == 200  # API_TOKEN unset -> gate no-ops
    data = {p["source"]: p for p in res.json()["data"]}
    assert set(data) == {"sreality", "remax"}
    assert data["sreality"]["overrides"] == {"detail_workers": 8, "detail_rate": 6.0}
    assert data["sreality"]["effective"]["detail_rate"] == 6.0
    assert data["sreality"]["baked_default"] is not None
    # parser-only portal: NULL overrides + no baked scraper default
    assert data["remax"]["overrides"] is None
    assert data["remax"]["baked_default"] is None


def test_put_portal_limits_merges_and_persists(client):
    res = client.put("/admin/portals/sreality/limits", json={"detail_rate": 9.0})
    assert res.status_code == 200
    body = res.json()
    # merge preserves the untouched key, updates the sent one
    assert body["overrides"] == {"detail_workers": 8, "detail_rate": 9.0}
    assert client._conn.portals["sreality"]["operational_limits"]["detail_rate"] == 9.0


def test_put_portal_limits_unknown_source_404(client):
    res = client.put("/admin/portals/nope/limits", json={"detail_rate": 9.0})
    assert res.status_code == 404


def test_put_portal_limits_no_fields_400(client):
    res = client.put("/admin/portals/sreality/limits", json={})
    assert res.status_code == 400


@pytest.mark.parametrize("payload", [
    {"detail_workers": 0},          # must be >= 1
    {"detail_rate": 0},             # must be > 0
    {"suspicious_stop_threshold": 1.5},  # must be in (0, 1]
    {"max_detail_per_run": 0},      # must be >= 1 or null
])
def test_put_portal_limits_bad_value_400(client, payload):
    res = client.put("/admin/portals/sreality/limits", json=payload)
    assert res.status_code == 400


def test_put_portal_limits_null_cap_means_unlimited(client):
    res = client.put("/admin/portals/sreality/limits", json={"max_detail_per_run": None})
    assert res.status_code == 200
    assert res.json()["overrides"]["max_detail_per_run"] is None
