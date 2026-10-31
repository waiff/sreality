"""The paths that spend the platform's money on a caller's say-so are admin-only (agent platform M0, #2059).

A trial account could open a building run (an LLM extraction, then one estimation per unit) and force a
cached LLM tool to recompute; both now take `require_admin`. Exercised through the real `verify_jwt` with
a minted token, so the 403 is the gate production runs, not an override.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import dependencies as deps  # noqa: E402
from api import main as api_main  # noqa: E402
from tests.api._jwt import mint  # noqa: E402
from tests.api._routes import method_routes  # noqa: E402

_MEMBER = {"Authorization": f"Bearer {mint({'sub': 'trial-user'})}"}
_ADMIN = {"Authorization": f"Bearer {mint({'sub': 'operator', 'app_metadata': {'is_admin': True}})}"}


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # The suite runs under the local-dev opt-out (tests/conftest.py); these gates are production's.
    monkeypatch.delenv("API_AUTH_OPTIONAL", raising=False)
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()
    api_main.app.dependency_overrides[deps.get_llm_client] = lambda: object()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def _building_routes() -> list[tuple[str, str]]:
    out = [(m, re.sub(r"\{[^}]+\}", "1", p)) for m, p, _r in method_routes() if p.startswith("/buildings")]
    assert len(out) >= 10, out
    return out


@pytest.mark.parametrize(("method", "path"), _building_routes())
def test_a_member_gets_403_on_every_building_route(client: Any, method: str, path: str) -> None:
    assert client.request(method, path, headers=_MEMBER, json={}).status_code == 403


def test_an_admin_passes_the_building_gate(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_main, "list_building_runs", lambda conn, **_kw: {"data": [], "count": 0})
    res = client.get("/buildings", headers=_ADMIN)
    assert res.status_code == 200, res.text


@pytest.mark.parametrize(("path", "body", "target"), [
    ("/tools/summarize_listing", {"sreality_id": 1, "force_refresh": True}, "summarize_listing"),
    ("/tools/compare_listing_images",
     {"sreality_id_a": 1, "sreality_id_b": 2, "force_refresh": True}, "compare_listing_images"),
])
def test_a_forced_refresh_is_admin_only(
    client: Any, monkeypatch: pytest.MonkeyPatch, path: str, body: dict, target: str,
) -> None:
    """A member still reads the cached answer; forcing a paid recompute takes an admin."""
    calls: list[bool] = []
    monkeypatch.setattr(api_main, target, lambda *_a, force_refresh, **_kw: calls.append(force_refresh) or {})
    assert client.post(path, headers=_MEMBER, json=body).status_code == 403
    assert calls == []
    assert client.post(path, headers=_MEMBER, json={**body, "force_refresh": False}).status_code == 200
    assert client.post(path, headers=_ADMIN, json=body).status_code == 200
    assert calls == [False, True]


def test_a_forced_refresh_under_the_local_dev_opt_out_is_still_refused(
    client: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API_AUTH_OPTIONAL=1 makes every caller anonymous, and an anonymous caller is no admin."""
    monkeypatch.setenv("API_AUTH_OPTIONAL", "1")
    monkeypatch.setattr(api_main, "summarize_listing", lambda *_a, **_kw: {})
    assert client.post("/tools/summarize_listing", json={"sreality_id": 1, "force_refresh": True}).status_code == 403
    assert client.post("/tools/summarize_listing", json={"sreality_id": 1}).status_code == 200
