"""Location W1v route tests — hermetic (no DB/HTTP), test_broker_routes idiom.

The router is admin-gated at the APIRouter level; overriding `verify_jwt` only
means every 403 below is the REAL `require_admin`'s decision. Toolkit functions
are monkeypatched, so these tests pin the HTTP layer: gating, status codes,
parameter passing and error mapping — not SQL.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import location_quality as routes
from location_data import operator_corrections as oc
from toolkit import location_labels, location_quality


def _client(claims: dict):
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: claims
    return TestClient(api_main.app)


@pytest.fixture()
def client():
    yield _client({"sub": "u-1", "email": "user@example.com"})
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def admin_client():
    yield _client({"sub": "u-0", "is_admin": True})
    api_main.app.dependency_overrides.clear()


def test_every_location_route_403s_a_plain_user(client):
    for method, path in [
        ("GET", "/location/quality/summary"),
        ("GET", "/location/quality/source/bezrealitky"),
        ("GET", "/location/quality/w1v-gate"),
        ("GET", "/location/listing/1"),
        ("GET", "/location/sample/bezrealitky"),
        ("POST", "/location/sample/bezrealitky/labels"),
        ("GET", "/location/sample/bezrealitky/score"),
        ("POST", "/location/corrections"),
    ]:
        res = client.request(method, path, json={} if method == "POST" else None)
        assert res.status_code == 403, (method, path, res.status_code)


def test_unknown_source_is_a_404_not_a_scan(admin_client, monkeypatch):
    called = []
    monkeypatch.setattr(location_quality, "source_overview",
                        lambda conn, source: called.append(source) or {"data": {}})
    res = admin_client.get("/location/quality/source/example-portal")
    assert res.status_code == 404
    assert called == []


def test_source_overview_passes_through(admin_client, monkeypatch):
    monkeypatch.setattr(
        location_quality, "source_overview",
        lambda conn, source: {"data": {"source": source}, "metadata": {}},
    )
    res = admin_client.get("/location/quality/source/bezrealitky")
    assert res.status_code == 200
    assert res.json()["data"]["source"] == "bezrealitky"


def test_gate_endpoint_shape(admin_client, monkeypatch):
    monkeypatch.setattr(
        location_quality, "w1v_gate",
        lambda conn: {"data": {"primary_pass": True}, "metadata": {}},
    )
    assert admin_client.get("/location/quality/w1v-gate").json()["data"]["primary_pass"] is True


def test_inspector_404_maps_none(admin_client, monkeypatch):
    monkeypatch.setattr(location_quality, "listing_inspector",
                        lambda conn, **kw: None)
    assert admin_client.get("/location/listing/999").status_code == 404


def test_labels_unknown_member_is_404(admin_client, monkeypatch):
    monkeypatch.setattr(location_labels, "save_labels",
                        lambda conn, source, listing_id, labels: False)
    res = admin_client.post(
        "/location/sample/bezrealitky/labels",
        json={"listing_id": 42, "labels": {"label_obec": "Brno"}},
    )
    assert res.status_code == 404
    assert "frozen" in res.json()["detail"]


def test_labels_validation_error_is_422(admin_client, monkeypatch):
    def boom(conn, source, listing_id, labels):
        raise ValueError("unknown label field 'label_bogus'")
    monkeypatch.setattr(location_labels, "save_labels", boom)
    res = admin_client.post(
        "/location/sample/bezrealitky/labels",
        json={"listing_id": 42, "labels": {"label_bogus": "x"}},
    )
    assert res.status_code == 422


def test_correction_maps_errors_and_resolves(admin_client, monkeypatch):
    submitted = {}

    def fake_submit(conn, **kw):
        submitted.update(kw)
        return {"listing_id": kw["listing_id"], "inserted": True, "restatement": False,
                "enqueued": True, "registry_echo": None, "resolved": False,
                "projection": None, "claim_type": kw["claim_type"],
                "value_text": kw["value_text"], "source": "bezrealitky"}

    monkeypatch.setattr(oc, "submit_correction", fake_submit)
    monkeypatch.setattr(oc, "resolve_now", lambda conn, listing_id: True)
    monkeypatch.setattr(oc, "read_projection",
                        lambda conn, listing_id: {"listing_id": listing_id,
                                                  "granularity": "address_point"})

    res = admin_client.post(
        "/location/corrections",
        json={"listing_id": 7, "claim_type": "street_name", "value_text": "Vodičkova"},
    )
    assert res.status_code == 200
    body = res.json()["data"]
    assert submitted["listing_id"] == 7
    assert body["resolved"] is True
    assert body["projection"]["granularity"] == "address_point"


def test_correction_unknown_listing_404(admin_client, monkeypatch):
    def raise_unknown(conn, **kw):
        raise oc.UnknownListingError("listing 7 does not exist")
    monkeypatch.setattr(oc, "submit_correction", raise_unknown)
    res = admin_client.post(
        "/location/corrections",
        json={"listing_id": 7, "claim_type": "street_name", "value_text": "X"},
    )
    assert res.status_code == 404


def test_correction_invalid_input_422(admin_client, monkeypatch):
    def raise_bad(conn, **kw):
        raise oc.CorrectionError("claim_type 'coordinate' not correctable")
    monkeypatch.setattr(oc, "submit_correction", raise_bad)
    res = admin_client.post(
        "/location/corrections",
        json={"listing_id": 7, "claim_type": "coordinate", "value_text": "1 2"},
    )
    assert res.status_code == 422
