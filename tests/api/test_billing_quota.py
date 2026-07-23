"""_agent_estimation_quota: the /billing/me block driving the extension's
"(zbývá X)" counter. Admin/legacy/no-account callers are unmetered (unlimited);
a real tenant reuses the metering resolver + monthly count."""

from __future__ import annotations

from typing import Any

import pytest

from api.routes import billing


def _quota(claims: dict, account_id: Any, monkeypatch) -> dict[str, Any]:
    import api.estimation_runs as er

    monkeypatch.setattr(er, "_resolve_entitlement", lambda conn, aid: ("trialing", True, 10))
    monkeypatch.setattr(er, "_count_agent_runs_this_month", lambda conn, aid: 3)
    return billing._agent_estimation_quota(object(), claims, account_id)


def test_real_tenant_is_metered_with_remaining(monkeypatch):
    out = _quota({"sub": "u", "app_metadata": {}}, "acct-1", monkeypatch)
    assert out == {
        "quota": 10, "used": 3, "remaining": 7, "is_trial": True, "metered": True,
    }


def test_remaining_never_negative(monkeypatch):
    import api.estimation_runs as er

    monkeypatch.setattr(er, "_resolve_entitlement", lambda conn, aid: ("active", True, 3))
    monkeypatch.setattr(er, "_count_agent_runs_this_month", lambda conn, aid: 5)
    out = billing._agent_estimation_quota(object(), {"sub": "u"}, "acct-1")
    assert out["remaining"] == 0 and out["metered"] is True


def test_legacy_token_is_unmetered():
    out = billing._agent_estimation_quota(object(), {"legacy": True, "is_admin": True}, "acct-1")
    assert out == {"quota": 0, "used": 0, "remaining": 0, "is_trial": False, "metered": False}


def test_admin_jwt_is_unmetered():
    out = billing._agent_estimation_quota(
        object(), {"sub": "u", "app_metadata": {"is_admin": True}}, "acct-1",
    )
    assert out["metered"] is False


def test_no_account_is_unmetered():
    out = billing._agent_estimation_quota(object(), {"sub": None}, None)
    assert out["metered"] is False
