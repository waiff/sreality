"""Tests for image_storage helpers (key generation, env-var validation)."""

from __future__ import annotations

import pytest

from scraper.image_storage import R2_ENV_VARS, image_key, is_configured


def test_image_key_pads_sequence():
    assert image_key(2836292428, 1) == "2836292428/0001.jpg"
    assert image_key(2836292428, 19) == "2836292428/0019.jpg"
    assert image_key(2836292428, 1234) == "2836292428/1234.jpg"


def test_image_key_handles_missing_sequence():
    assert image_key(123, None) == "123/0000.jpg"


def test_is_configured_requires_all_vars(monkeypatch):
    for name in R2_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert is_configured() is False

    for name in R2_ENV_VARS[:-1]:
        monkeypatch.setenv(name, "x")
    assert is_configured() is False

    monkeypatch.setenv(R2_ENV_VARS[-1], "x")
    assert is_configured() is True


def test_from_env_raises_when_missing(monkeypatch):
    from scraper.image_storage import R2Client

    for name in R2_ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="R2_ACCOUNT_ID"):
        R2Client.from_env()


def test_download_image_appends_transform(monkeypatch):
    """Bare CDN URLs 401 without the render-transform; the downloader adds it."""
    import scraper.image_storage as image_storage

    captured: list[str] = []

    class _Resp:
        content = b"jpegbytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        image_storage.requests, "get",
        lambda url, timeout=15.0: (captured.append(url), _Resp())[1],
    )
    assert image_storage.download_image("https://d18-a.sdn.cz/x/y.jpeg") == b"jpegbytes"
    assert captured == ["https://d18-a.sdn.cz/x/y.jpeg?fl=res,749,562,3|shr,,20|jpg,90"]


def test_download_image_idempotent_on_existing_fl(monkeypatch):
    """Pre-rebuild stored URLs already carry the param — don't double-append."""
    import scraper.image_storage as image_storage

    captured: list[str] = []

    class _Resp:
        content = b""

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        image_storage.requests, "get",
        lambda url, timeout=15.0: (captured.append(url), _Resp())[1],
    )
    existing = "https://d18-a.sdn.cz/x/y.jpeg?fl=res,749,562,3|shr,,20|jpg,90"
    image_storage.download_image(existing)
    assert captured == [existing]
