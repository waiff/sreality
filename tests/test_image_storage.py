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


def test_with_transform_completes_rot_prefix_chain():
    """sreality now ships some URLs with a prefix chain '?fl=rot,<deg>,0|'
    (trailing pipe). The CDN 400s it as-is AND with the pipe stripped; only the
    completed chain returns bytes. The rot op must be preserved — completing
    without it returns 200 but stores the photo unrotated (curl-verified)."""
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, _with_transform

    url = "https://d18-a.sdn.cz/d_18/c_img_a/x.jpeg?fl=rot,180,0|"
    assert _with_transform(url) == (
        "https://d18-a.sdn.cz/d_18/c_img_a/x.jpeg?fl=rot,180,0|"
        + IMAGE_TRANSFORM_OPS
    )


def test_with_transform_completes_rot_chain_without_trailing_pipe():
    """A rot prefix without the trailing pipe gets exactly one pipe separator."""
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, _with_transform

    url = "https://d18-a.sdn.cz/d_18/c_img_a/x.jpeg?fl=rot,90,0"
    assert _with_transform(url) == url + "|" + IMAGE_TRANSFORM_OPS


def test_with_transform_leaves_legacy_complete_chain_untouched():
    """Pre-rebuild stored URLs carry a complete 'res,'-bearing chain — the
    823k-row legacy cohort must download verbatim."""
    from scraper.image_storage import _with_transform

    legacy = "https://d18-a.sdn.cz/x/y.jpeg?fl=res,749,562,3|shr,,20|jpg,90"
    assert _with_transform(legacy) == legacy


def test_download_image_leaves_non_sreality_url_untouched(monkeypatch):
    """The render-transform is sreality-CDN-only; bazos (and other portals')
    image URLs must download verbatim (they 404 on the sreality query)."""
    import scraper.image_storage as image_storage

    captured: list[str] = []

    class _Resp:
        content = b"bazosbytes"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        image_storage.requests, "get",
        lambda url, timeout=15.0: (captured.append(url), _Resp())[1],
    )
    bazos = "https://www.bazos.cz/img/1/123/456.jpg"
    assert image_storage.download_image(bazos) == b"bazosbytes"
    assert captured == [bazos]  # no transform appended
