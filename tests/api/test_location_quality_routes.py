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


class _NoDbTxn:
    def __enter__(self) -> "_NoDbTxn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _NoDbConn:
    """Enough of a connection for a route that validates before it reads."""

    def transaction(self) -> _NoDbTxn:
        return _NoDbTxn()


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


# ---------------------------------------------------------------------------
# compare — the dark old-vs-new review surface (location W6).
# ---------------------------------------------------------------------------

from toolkit import location_compare  # noqa: E402


_COMPARE_PATHS = [
    "/location/compare/scope",
    "/location/compare/units?level=obec&parent_kod=3100",
    "/location/compare/unit?level=obec&code=554782",
    "/location/compare/streets?obec_kod=554782",
    "/location/compare/map?west=14&south=50&east=14.5&north=50.5",
    "/location/compare/radius?lat=50.08&lng=14.44&radius_m=1000",
]


def test_every_compare_route_403s_a_plain_user(client):
    for path in _COMPARE_PATHS:
        assert client.get(path).status_code == 403, path


def test_compare_scope_defaults_to_praha_and_stredocesky(admin_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        location_compare, "scope",
        lambda conn, kraje: (seen.update(kraje=kraje), {"kraje": kraje})[1],
    )
    assert admin_client.get("/location/compare/scope").status_code == 200
    assert seen["kraje"] == [19, 27]


def test_compare_scope_accepts_a_kraje_list(admin_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        location_compare, "scope",
        lambda conn, kraje: (seen.update(kraje=kraje), {"kraje": kraje})[1],
    )
    admin_client.get("/location/compare/scope?kraje=19,%2027,31")
    assert seen["kraje"] == [19, 27, 31]


@pytest.mark.parametrize("bad", ["abc", "19,abc", "19;27"])
def test_a_non_integer_kraje_is_a_400(admin_client, monkeypatch, bad):
    monkeypatch.setattr(location_compare, "scope", lambda conn, kraje: {})
    assert admin_client.get(f"/location/compare/scope?kraje={bad}").status_code == 400


def test_a_junk_level_is_a_400_on_both_level_taking_routes(admin_client):
    assert admin_client.get(
        "/location/compare/units?level=kraj&parent_kod=19"
    ).status_code == 400
    assert admin_client.get(
        "/location/compare/unit?level=ulice&code=1"
    ).status_code == 400


def test_an_inverted_map_bbox_is_a_400(admin_client, monkeypatch):
    monkeypatch.setattr(location_compare, "map_rows", lambda conn, **kw: {})
    res = admin_client.get("/location/compare/map?west=14.5&south=50&east=14&north=50.5")
    assert res.status_code == 400


def test_an_out_of_range_radius_is_a_400(admin_client, monkeypatch):
    # The CZ envelope is read from location_constants (migration 380), never a
    # literal, so the out-of-country arm needs the constant served.
    monkeypatch.setattr(
        location_compare,
        "cz_bbox",
        lambda conn: {"west": 12.0, "south": 48.0, "east": 19.0, "north": 51.5},
    )
    monkeypatch.setattr(location_compare, "_timeout", lambda conn: None)
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: _NoDbConn()
    assert admin_client.get(
        "/location/compare/radius?lat=50.08&lng=14.44&radius_m=99999"
    ).status_code == 400
    assert admin_client.get(
        "/location/compare/radius?lat=0&lng=0&radius_m=1000"
    ).status_code == 400


def test_compare_limits_are_clamped_by_the_query_constraints(admin_client, monkeypatch):
    monkeypatch.setattr(location_compare, "unit_detail", lambda conn, **kw: {})
    assert admin_client.get(
        "/location/compare/unit?level=obec&code=1&limit=999999"
    ).status_code == 422
    assert admin_client.get(
        "/location/compare/unit?level=obec&code=1&limit=0"
    ).status_code == 422


def test_compare_unit_passes_level_code_kraje_and_limit_through(admin_client, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        location_compare, "unit_detail",
        lambda conn, **kw: seen.update(kw) or {"level": kw["level"]},
    )
    res = admin_client.get(
        "/location/compare/unit?level=street&code=42&kraje=19&limit=7"
    )
    assert res.status_code == 200
    assert seen == {"level": "street", "code": 42, "kraje": [19], "limit": 7}
