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
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def presigned_get(
        self, key: str, expires_in: int = 0, anchor_seconds: int = 0
    ) -> str:
        self.calls.append((key, expires_in, anchor_seconds))
        return f"https://example.r2.cloudflarestorage.com/bucket/{key}?sig=abc"


@pytest.fixture()
def fake_r2():
    return _FakeR2()


@pytest.fixture()
def client(monkeypatch, fake_r2):
    monkeypatch.setattr(image_storage, "is_configured", lambda: True)
    monkeypatch.setattr(image_storage.R2Client, "from_env", classmethod(lambda cls, **_kw: fake_r2))
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


def test_key_whitelist_accepts_what_the_writer_actually_mints():
    """The route's whitelist and the key writer are in two territories and nothing
    else ties them together — a scheme change that forgets _KEY_RE 404s every photo
    silently. Assert the actual producer's output against the actual gate."""
    from scraper.image_storage import image_key

    assert images_route._KEY_RE.match(image_key(443628, 226547358))


@pytest.mark.parametrize("key", ["2872083276/0001.jpg\n", "img/443628/226547358.jpg\n"])
def test_trailing_newline_rejected(client, key):
    # `\Z`, not `$` — a security predicate must not accept a trailing newline.
    assert not images_route._KEY_RE.match(key)


def test_current_namespaced_key_allowed(client):
    # `img/{listing_id}/{image_id}.jpg` — the collision-proof scheme every newly
    # stored image takes; the two legacy shapes above stay serveable forever.
    res = client.get("/images/img/443628/226547358.jpg", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"].endswith("img/443628/226547358.jpg?sig=abc")


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
        "img/custom-attachments/1.jpg",           # namespace is not a free prefix
        "img/443628/226547358.pdf",               # wrong extension under img/
        "img/443628.jpg",                         # missing the image-id segment
        "x/img/443628/226547358.jpg",             # not anchored at the namespace
    ],
)
def test_non_image_keys_rejected(client, key):
    res = client.get(f"/images/{key}", follow_redirects=False)
    assert res.status_code == 404


def test_presign_is_anchored_by_default(client, fake_r2):
    """The redirect target has to be stable across the hourly re-mint, or the browser
    re-downloads every photo it already holds."""
    client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    _key, expires_in, anchor = fake_r2.calls[-1]
    assert anchor == images_route._PRESIGN_ANCHOR_DEFAULT
    assert expires_in == images_route._PRESIGN_TTL
    # The redirect itself must NOT be cached longer just because the target is stable —
    # that short TTL is what makes a credential rotation self-heal within the hour.
    assert images_route._REDIRECT_MAX_AGE == 3600


def test_anchor_kill_switch_reverts_to_per_request_signing(client, fake_r2, monkeypatch):
    monkeypatch.setenv("IMAGE_PRESIGN_ANCHOR_SECONDS", "0")
    client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert fake_r2.calls[-1][2] == 0


def test_anchor_override_is_capped_below_the_presign_ttl(client, fake_r2, monkeypatch):
    """An anchor at or past the TTL would hand out already-expired URLs."""
    monkeypatch.setenv("IMAGE_PRESIGN_ANCHOR_SECONDS", "9999999")
    client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert fake_r2.calls[-1][2] == images_route._PRESIGN_TTL // 2


def test_garbage_anchor_override_falls_back_to_the_default(client, fake_r2, monkeypatch):
    monkeypatch.setenv("IMAGE_PRESIGN_ANCHOR_SECONDS", "not-a-number")
    client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert fake_r2.calls[-1][2] == images_route._PRESIGN_ANCHOR_DEFAULT


def test_unconfigured_storage_returns_503(client, monkeypatch):
    monkeypatch.setattr(image_storage, "is_configured", lambda: False)
    images_route._client = None
    res = client.get("/images/2872083276/0001.jpg", follow_redirects=False)
    assert res.status_code == 503
