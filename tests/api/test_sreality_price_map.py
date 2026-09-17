"""The Cenová mapa deep-link proxy. Hermetic — sreality's suggest is mocked with
the userData shape measured live on 2026-09-17."""

from __future__ import annotations

from typing import Any

import pytest
import requests

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api import sreality_price_map as spm

# Popelky Biliánové 534, Králův Dvůr — the listing point.
KD_LAT, KD_LNG = 49.945963, 14.037261

KD_CHAIN = {
    "region_seo_name": "stredocesky-kraj", "region_id": 11,
    "district_seo_name": "beroun", "district_id": 49,
    "municipality_seo_name": "kraluv-dvur", "municipality_id": 3605,
}
STREET = {
    **KD_CHAIN, "entityType": "street", "id": 78029,
    "street_seo_name": "popelky-bilianove", "street_id": 78029,
    "ward_seo_name": "kraluv-dvur", "ward_id": 0,
    "suggestFirstRow": "ulice Popelky Biliánové",
    "latitude": 49.94533787036325, "longitude": 14.037372722238183,
}
MUNICIPALITY = {
    **KD_CHAIN, "entityType": "municipality", "id": 3605,
    "street_id": 0, "ward_id": 0, "suggestFirstRow": "Králův Dvůr",
    "latitude": 49.9498, "longitude": 14.0345,
}
WARD = {
    **KD_CHAIN, "entityType": "ward", "id": 5592,
    "ward_seo_name": "levin", "ward_id": 5592, "suggestFirstRow": "Levín",
    "latitude": 49.9284, "longitude": 14.0014,
}

KD_URL = (
    "https://www.sreality.cz/cenova-mapa/hledani/byty/"
    "stredocesky-kraj-11/beroun-49/kraluv-dvur-3605"
)


def _results(*items: dict[str, Any]) -> dict[str, Any]:
    return {"results": [{"category": "x", "userData": u} for u in items]}


class Calls(list):
    """The params of every suggest ask, plus the canned answer per category."""

    def __init__(self) -> None:
        super().__init__()
        self.answers: dict[str, dict[str, Any]] = {}


@pytest.fixture()
def calls(monkeypatch) -> Calls:
    seen = Calls()

    def fake(url: str, params: dict[str, Any]) -> dict[str, Any]:
        seen.append(params)
        return seen.answers.get(params["category"], {"results": []})

    monkeypatch.setattr(spm, "_http_get_json", fake)
    spm.clear_cache()
    yield seen
    spm.clear_cache()


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Popelky Biliánové 534, Králův Dvůr", ("Popelky Biliánové", "Králův Dvůr")),
        ("Rašínovo nábřeží 410/32, Praha", ("Rašínovo nábřeží", "Praha")),
        ("Nádražní 12a, Beroun", ("Nádražní", "Beroun")),
        # Digits that belong to the NAME stay.
        ("28. října, Beroun", ("28. října", "Beroun")),
        ("Třída 1. máje 15, Liberec", ("Třída 1. máje", "Liberec")),
        ("Levín, Králův Dvůr", ("Levín", "Králův Dvůr")),
        ("Králův Dvůr", (None, "Králův Dvůr")),
    ],
)
def test_split_label_cuts_only_the_house_number(label: str, expected: tuple) -> None:
    assert spm.split_label(label) == expected


def test_a_street_resolves_to_the_street_view(calls) -> None:
    calls.answers["street_cz,ward_cz"] = _results(STREET)

    out = spm.resolve("Popelky Biliánové 534, Králův Dvůr", KD_LAT, KD_LNG)

    assert out == {
        "url": f"{KD_URL}?ulice=popelky-bilianove-78029",
        "level": "street",
        "name": "ulice Popelky Biliánové",
    }
    # With the house number the suggest finds nothing (measured) — it must be cut.
    assert calls[0]["phrase"] == "Popelky Biliánové, Králův Dvůr"
    assert len(calls) == 1


def test_a_part_of_town_resolves_to_its_path_segment(calls) -> None:
    calls.answers["street_cz,ward_cz"] = _results(WARD)

    # 3.1 km from the part's own centre point: past a street's cap, inside a
    # part of town's — a part is an area, not a point on a line.
    out = spm.resolve("Levín, Králův Dvůr", 49.9531, 14.0212)

    assert out["url"] == f"{KD_URL}/levin-5592"
    assert out["level"] == "ward"


def test_a_street_is_held_to_the_tighter_cap(calls) -> None:
    street_3km_off = {**STREET, "latitude": 49.9284, "longitude": 14.0014}
    calls.answers["street_cz,ward_cz"] = _results(street_3km_off)
    calls.answers["municipality_cz"] = _results(MUNICIPALITY)

    out = spm.resolve("Popelky Biliánové 534, Králův Dvůr", 49.9531, 14.0212)

    assert out["level"] == "municipality"


def test_falls_back_to_the_town_when_the_street_is_unknown(calls) -> None:
    calls.answers["municipality_cz"] = _results(MUNICIPALITY)

    out = spm.resolve("Neznámá 1, Králův Dvůr", KD_LAT, KD_LNG)

    assert out["url"] == KD_URL
    assert out["level"] == "municipality"
    assert [c["phrase"] for c in calls] == ["Neznámá, Králův Dvůr", "Králův Dvůr"]


def test_a_town_only_label_asks_for_the_town_once(calls) -> None:
    calls.answers["municipality_cz"] = _results(MUNICIPALITY)

    assert spm.resolve("Králův Dvůr", KD_LAT, KD_LNG)["url"] == KD_URL
    assert len(calls) == 1


def test_same_name_towns_are_told_apart_by_distance(calls) -> None:
    """ "Nová Ves" alone is five municipalities; the listing's point decides."""
    far = {**MUNICIPALITY, "municipality_seo_name": "nova-ves-u-lestiny",
           "municipality_id": 5044, "latitude": 49.7859, "longitude": 15.4039}
    near = {**MUNICIPALITY, "municipality_seo_name": "nova-ves-pod-plesi",
            "municipality_id": 4027, "district_seo_name": "pribram",
            "district_id": 58, "latitude": 49.8318, "longitude": 14.2752}
    calls.answers["municipality_cz"] = _results(far, near)

    out = spm.resolve("Nová Ves", 49.83, 14.28)

    assert out["url"].endswith("/pribram-58/nova-ves-pod-plesi-4027")


def test_no_link_when_every_candidate_is_someone_elses_place(calls) -> None:
    """A same-name street 40 km away is not this listing's street, and a town
    beyond the cap is not its town — no link beats a wrong one."""
    far_street = {**STREET, "latitude": 50.3, "longitude": 14.5}
    far_town = {**MUNICIPALITY, "latitude": 50.3, "longitude": 14.5}
    calls.answers["street_cz,ward_cz"] = _results(far_street)
    calls.answers["municipality_cz"] = _results(far_town)

    assert spm.resolve("Popelky Biliánové 534, Králův Dvůr", KD_LAT, KD_LNG) == {
        "url": None, "level": None, "name": None,
    }


def test_a_broken_chain_or_zero_id_is_not_linked(calls) -> None:
    # Praha's streets carry ward_id 0 — a ward result with id 0 has no path.
    zero_ward = {**WARD, "ward_id": 0}
    no_district = {**MUNICIPALITY, "district_id": None}
    calls.answers["street_cz,ward_cz"] = _results(zero_ward)
    calls.answers["municipality_cz"] = _results(no_district)

    assert spm.resolve("Levín, Králův Dvůr", 49.9284, 14.0014)["url"] is None


def test_results_are_cached(calls) -> None:
    calls.answers["street_cz,ward_cz"] = _results(STREET)

    spm.resolve("Popelky Biliánové 534, Králův Dvůr", KD_LAT, KD_LNG)
    spm.resolve("Popelky Biliánové 534, Králův Dvůr", KD_LAT, KD_LNG)

    assert len(calls) == 1


@pytest.fixture()
def client():
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.get_sreality_client] = lambda: object()
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def test_route_returns_the_resolution(client, calls) -> None:
    calls.answers["street_cz,ward_cz"] = _results(STREET)

    res = client.get(
        "/maps/sreality-price-map",
        params={"label": "Popelky Biliánové 534, Králův Dvůr", "lat": KD_LAT, "lng": KD_LNG},
    )

    assert res.status_code == 200
    assert res.json()["url"] == f"{KD_URL}?ulice=popelky-bilianove-78029"


def test_route_503s_when_sreality_is_down(client, monkeypatch) -> None:
    spm.clear_cache()
    resp = requests.Response()
    resp.status_code = 502

    def down(url: str, params: dict[str, Any]) -> dict[str, Any]:
        raise requests.HTTPError("502", response=resp)

    monkeypatch.setattr(spm, "_http_get_json", down)
    res = client.get(
        "/maps/sreality-price-map",
        params={"label": "Králův Dvůr", "lat": KD_LAT, "lng": KD_LNG},
    )
    assert res.status_code == 503


def test_route_validates_its_inputs(client) -> None:
    assert client.get("/maps/sreality-price-map", params={"label": "x", "lat": 95, "lng": 14}).status_code == 422
    assert client.get("/maps/sreality-price-map", params={"lat": 50, "lng": 14}).status_code == 422
