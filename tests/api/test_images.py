"""Tests for GET /images/{key} — the public presigned-R2 redirect.

The endpoint is unauthenticated (like /health), redirects a listing-image key
to a presigned R2 URL, and refuses any key that isn't the listing-image shape
so it can never presign the operator-private `custom-attachments/` uploads that
share the bucket.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import main as api_main
from api.routes import images as images_route
from scraper import image_storage


class _FakeR2:
    def presigned_get(self, key: str, expires_in: int = 0) -> str:
        return f"https://example.r2.cloudflarestorage.com/bucket/{key}?sig=abc"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **_kw: _FakeR2()))
    images_route._client = None  # reset the module-level lazy singleton
    yield TestClient(api_main.app)
    images_route._client = None


def test_valid_key_redirects_to_presigned_url(client):
    res = client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"].endswith("2872083276/0001.jpg?sig=abc")
    assert "max-age" in res.headers.get("cache-control", "")


def test_negative_id_key_allowed(client):
    # Non-sreality portals use synthetic negative native ids.
    res = client.get("/images/-4671/0009.jpg", follow_redirects=False)
    assert res.status_code == 302


def test_public_even_when_token_set(client, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "secret-token-xyz")
    res = client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert res.status_code == 302


@pytest.mark.parametrize(
    "key",
    [
        "custom-attachments/building/1/abc.pdf",  # operator-private uploads
        "2872083276/0001.png",                    # wrong extension
        "../etc/passwd",                          # traversal
        "2872083276",                             # no sequence
        "foo/0001.jpg",                           # non-numeric id
    ],
)
def test_non_image_keys_rejected(client, key):
    res = client.get(f"/images/{key}", follow_redirects=False)
    assert res.status_code == 404


def test_unconfigured_storage_returns_503(client, monkeypatch):
    monkeypatch.setattr(image_storage, "is_configured", lambda: False)
    images_route._client = None
    res = client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert res.status_code == 503
