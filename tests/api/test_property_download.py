"""Tests for GET /properties/{id}/download.zip — the property page's download button:
a PDF of the ad plus the canonical ad's stored photos, zipped server-side.

The renderer is replaced by a recorder here, so these tests pin what the route READS
and HANDS the sheet (the PII policy, the page's broker order, the cover photo) and how
it packs the zip; tests/api/test_property_sheet.py renders real PDFs.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import dependencies as deps
from api import main as api_main
from api.routes import images as images_route
from api.routes import property_download
from scraper import image_storage
from toolkit import brokers as brokers_mod

PROPERTY = {"property_id": 7, "listing_id": 42, "is_active": True, "display_label": "Nusle, Praha"}
ADS = [
    {"id": 41, "source": "idnes", "is_active": True},
    {"id": 42, "source": "sreality", "is_active": True},
    {"id": 43, "source": "bazos", "is_active": False},
]
BROKER_ROWS = [
    {"listing_id": 41, "broker_id": 2, "broker_display_name": "Petr", "primary_email": "p@x.cz",
     "primary_phone": "420777000111"},
    {"listing_id": 42, "broker_id": 1, "broker_display_name": "Jana", "primary_email": "j@x.cz",
     "primary_phone": "420777000222"},
    {"listing_id": 43, "broker_id": 3, "broker_display_name": "Old", "primary_email": None,
     "primary_phone": None},
]


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.result: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple) -> None:
        self.conn.statements.append((" ".join(sql.split()), params))
        if "from properties_public" in sql:
            self.result = [self.conn.prop] if self.conn.prop else []
        elif "from property_sources_public" in sql:
            self.result = self.conn.ads
        elif "from images" in sql:
            self.result = [{"storage_path": k} for k in self.conn.keys]
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> dict[str, Any] | None:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.result


class _Conn:
    def __init__(self) -> None:
        self.prop: dict[str, Any] | None = dict(PROPERTY)
        self.ads = [dict(a) for a in ADS]
        self.keys: list[str] = []
        self.statements: list[tuple[str, tuple]] = []

    def cursor(self, row_factory: Any = None) -> _Cursor:
        return _Cursor(self)


class _FakeR2:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def download_bytes(self, key: str) -> bytes:
        return self.objects[key]


@pytest.fixture()
def fake_r2(monkeypatch):
    r2 = _FakeR2()
    monkeypatch.setattr(image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **_kw: r2))
    images_route._client = None  # reset the module-level lazy singleton
    yield r2
    images_route._client = None


@pytest.fixture()
def sheet(monkeypatch):
    """Records the renderer's inputs instead of drawing a PDF."""
    calls: list[dict[str, Any]] = []

    def _render(prop, ads, shown, **kw):
        calls.append({"prop": prop, "ads": ads, "brokers": shown, **kw})
        return b"%PDF-fake"

    monkeypatch.setattr(property_download, "render_property_sheet", _render)
    monkeypatch.setattr(
        brokers_mod, "listing_brokers",
        lambda conn, ids: {"data": [dict(r) for r in BROKER_ROWS if r["listing_id"] in ids]},
    )
    return calls


@pytest.fixture()
def claims():
    return {"sub": "u1"}


@pytest.fixture()
def conn(claims):
    c = _Conn()
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: c
    api_main.app.dependency_overrides[deps.verify_jwt] = lambda: claims
    yield c
    api_main.app.dependency_overrides.clear()


@pytest.fixture()
def client():
    return TestClient(api_main.app)


def test_zip_holds_the_sheet_then_every_stored_photo_in_gallery_order(client, fake_r2, sheet, conn):
    conn.keys = ["42/0003.jpg", "42/0001.jpg"]
    fake_r2.objects = {"42/0003.jpg": b"third", "42/0001.jpg": b"first"}
    res = client.get("/properties/7/download.zip")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert 'filename="property-7.zip"' in res.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(res.content))
    # Entries follow the query's order (the gallery's), not the storage keys.
    assert archive.namelist() == ["property-7.pdf", "01.jpg", "02.jpg"]
    assert archive.read("property-7.pdf") == b"%PDF-fake"
    assert archive.read("01.jpg") == b"third"
    # The photos are the canonical ad's, and the gallery's first is the sheet's cover.
    assert ("select storage_path from images where listing_id = %s and storage_path is not null"
            " order by sequence nulls last, id", (42,)) in conn.statements
    assert sheet[0]["cover"] == b"third"


def test_a_property_without_stored_photos_still_gets_its_sheet(client, sheet, conn):
    res = client.get("/properties/7/download.zip")
    assert res.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(res.content)).namelist() == ["property-7.pdf"]
    assert sheet[0]["cover"] is None


def test_unknown_or_merged_away_property_is_404(client, sheet, conn):
    conn.prop = None
    assert client.get("/properties/7/download.zip").status_code == 404


def test_fails_whole_when_a_photo_cannot_be_read(client, fake_r2, sheet, conn):
    """A half-filled zip would pass for the full set — fail loudly instead."""
    conn.keys = ["42/0001.jpg", "42/0002.jpg"]
    fake_r2.objects = {"42/0001.jpg": b"first"}
    assert client.get("/properties/7/download.zip").status_code == 502


def test_brokers_follow_the_page_order_active_ads_only(client, sheet, conn):
    client.get("/properties/7/download.zip")
    call = sheet[0]
    # The canonical ad's broker first, then the other active ads'; the inactive ad's dropped.
    assert [b["broker_id"] for b in call["brokers"]] == [1, 2]
    assert call["brokers_from_inactive"] is False


def test_with_no_active_ad_every_ads_broker_shows_flagged(client, sheet, conn):
    for ad in conn.ads:
        ad["is_active"] = False
    client.get("/properties/7/download.zip")
    assert [b["broker_id"] for b in sheet[0]["brokers"]] == [1, 2, 3]
    assert sheet[0]["brokers_from_inactive"] is True


def test_a_non_admin_gets_masked_broker_contacts(client, sheet, conn):
    client.get("/properties/7/download.zip")
    jana = sheet[0]["brokers"][0]
    assert "primary_email" not in jana and "primary_phone" not in jana
    assert jana["has_email"] is True and jana["has_phone"] is True


def test_an_admin_gets_the_contact_values(client, sheet, conn, claims):
    claims["app_metadata"] = {"is_admin": True}
    client.get("/properties/7/download.zip")
    assert sheet[0]["brokers"][0]["primary_phone"] == "420777000222"


def test_never_reads_the_unmasked_broker_columns_of_properties_public():
    """properties_public carries broker_email / broker_phone, which bypass the PII policy."""
    assert "broker" not in property_download._PROPERTY_COLS


def test_requires_a_signed_in_user(client):
    """It proxies photo bytes and broker data through the API: the bundle token is not enough."""
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: _Conn()
    try:
        assert client.get("/properties/7/download.zip").status_code == 401
    finally:
        api_main.app.dependency_overrides.clear()
