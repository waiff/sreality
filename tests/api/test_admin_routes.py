"""Tests for /admin/* — skills + app_settings + tools endpoints.

The whole prefix is exempted from the API_TOKEN bearer gate per
CLAUDE.md rule #8 (private Railway URL is the security perimeter).
We confirm that exemption here, plus the happy-path read / update
flows.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main


class _InMemorySkill:
    """One row in the in-memory skills store. Mutable, mimics the DB."""
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


_PROMPT = "you are an analyst"


def _seed_skill() -> _InMemorySkill:
    return _InMemorySkill(
        name="rental_estimator_v1",
        description="d",
        system_prompt=_PROMPT,
        allowed_tools=[
            "find_comparables_relaxed",
            "analyze_distribution",
            "record_estimate",
        ],
        preferred_model={
            "anthropic": "claude-sonnet-4-5",
            "gemini": "gemini-2.5-pro",
        },
        limits={
            "max_iterations": 12,
            "max_cost_usd": 1.0,
            "wall_clock_timeout_s": 120.0,
        },
        updated_at="2026-05-11T00:00:00+00:00",
    )


@pytest.fixture()
def store():
    skills: dict[str, _InMemorySkill] = {"rental_estimator_v1": _seed_skill()}
    app_settings: dict[str, dict[str, Any]] = {
        "llm_parse_model": {
            "value": "claude-sonnet-4-5",
            "description": "URL parser model",
            "updated_at": "2026-05-11T00:00:00+00:00",
        },
    }
    return {"skills": skills, "app_settings": app_settings}


@pytest.fixture()
def client(monkeypatch, store):
    from api import routes
    admin = routes.admin

    def fake_list_skills(conn, *, include_archived=False):
        from api.skills import Skill, SkillLimits
        return [
            Skill(
                name=s.name, description=s.description,
                system_prompt=s.system_prompt,
                allowed_tools=list(s.allowed_tools),
                preferred_model=dict(s.preferred_model),
                limits=SkillLimits(**s.limits),
                updated_at=s.updated_at,
            )
            for s in store["skills"].values()
        ]

    def fake_load_skill(conn, name):
        from api.skills import Skill, SkillLimits, SkillNotFound
        if name not in store["skills"]:
            raise SkillNotFound(f"skill {name!r} not found")
        s = store["skills"][name]
        return Skill(
            name=s.name, description=s.description,
            system_prompt=s.system_prompt,
            allowed_tools=list(s.allowed_tools),
            preferred_model=dict(s.preferred_model),
            limits=SkillLimits(**s.limits),
            updated_at=s.updated_at,
        )

    def fake_update_skill(conn, name, fields, *, updated_by=None):
        from api.skills import SkillNotFound, SkillValidationError
        if name not in store["skills"]:
            raise SkillNotFound(f"skill {name!r} not found")
        s = store["skills"][name]
        for k, v in fields.items():
            if k == "allowed_tools":
                if "boguscallout" in (v or []):
                    raise SkillValidationError("unknown tool 'boguscallout'")
                s.allowed_tools = list(v)
            else:
                setattr(s, k, v)
        return fake_load_skill(conn, name)

    monkeypatch.setattr(admin, "list_skills", fake_list_skills)
    monkeypatch.setattr(admin, "load_skill", fake_load_skill)
    monkeypatch.setattr(admin, "update_skill", fake_update_skill)

    class _AppSettingsCursor:
        def __init__(self, parent: "_AppSettingsConn") -> None:
            self._parent = parent
            self._last: list[Any] | None = None
        def __enter__(self): return self
        def __exit__(self, *exc): return None
        def execute(self, sql, params=()):
            sql_norm = " ".join(sql.split()).lower()
            if sql_norm.startswith("select key, value, description, updated_at from app_settings order by key"):
                self._parent.last_rows = [
                    (k, v["value"], v["description"], v["updated_at"])
                    for k, v in store["app_settings"].items()
                ]
                self._last = None
            elif sql_norm.startswith("select key, value, description, updated_at from app_settings where key"):
                key = params[0] if isinstance(params, tuple) else params.get("key")
                row = store["app_settings"].get(key)
                self._last = (
                    (key, row["value"], row["description"], row["updated_at"])
                    if row is not None else None
                )
            elif sql_norm.startswith("update app_settings"):
                key = params[2]
                value = json.loads(params[0])
                store["app_settings"][key]["value"] = value
                self._last = None
        def fetchone(self):
            return self._last
        def fetchall(self):
            return list(self._parent.last_rows)

    class _AppSettingsConn:
        def __init__(self) -> None:
            self.last_rows: list[Any] = []
        def cursor(self): return _AppSettingsCursor(self)
        def transaction(self):
            from contextlib import contextmanager
            @contextmanager
            def _ctx():
                yield self
            return _ctx()

    fake_conn = _AppSettingsConn()
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: fake_conn
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_get_skills_returns_list(client):
    res = client.get("/admin/skills")
    assert res.status_code == 200
    data = res.json()["data"]
    assert any(s["name"] == "rental_estimator_v1" for s in data)


def test_get_one_skill(client):
    res = client.get("/admin/skills/rental_estimator_v1")
    assert res.status_code == 200
    skill = res.json()
    assert skill["name"] == "rental_estimator_v1"
    assert "system_prompt" in skill


def test_get_skill_404(client):
    res = client.get("/admin/skills/nope")
    assert res.status_code == 404


def test_put_skill_persists_change(client):
    res = client.put(
        "/admin/skills/rental_estimator_v1",
        json={"system_prompt": "you are a fox"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["system_prompt"] == "you are a fox"
    res2 = client.get("/admin/skills/rental_estimator_v1")
    assert res2.json()["system_prompt"] == "you are a fox"


def test_put_skill_rejects_invalid_tool(client):
    res = client.put(
        "/admin/skills/rental_estimator_v1",
        json={"allowed_tools": ["boguscallout"]},
    )
    assert res.status_code == 400


def test_get_app_settings_lists_keys(client):
    res = client.get("/admin/app_settings")
    assert res.status_code == 200
    data = res.json()["data"]
    assert any(r["key"] == "llm_parse_model" for r in data)


def test_put_app_setting_persists(client):
    res = client.put("/admin/app_settings/llm_parse_model", json={"value": "claude-sonnet-4-6"})
    assert res.status_code == 200, res.text
    follow = client.get("/admin/app_settings/llm_parse_model")
    assert follow.json()["value"] == "claude-sonnet-4-6"


def test_admin_tools_lists_agent_registry(client):
    res = client.get("/admin/tools")
    assert res.status_code == 200
    names = [t["name"] for t in res.json()["data"]]
    assert "find_comparables_relaxed" in names
    assert "record_estimate" in names


def test_admin_routes_exempt_from_bearer_gate(client, monkeypatch):
    """Even with API_TOKEN set, /admin/* must respond without an Authorization header."""
    monkeypatch.setenv("API_TOKEN", "secret-xyz")
    res = client.get("/admin/skills")
    assert res.status_code == 200
    res = client.get("/admin/tools")
    assert res.status_code == 200
