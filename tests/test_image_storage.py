"""Tests for image_storage helpers (key generation, env-var validation)."""

from __future__ import annotations

import pytest

from scraper.image_storage import R2_ENV_VARS, image_key, is_configured


class _StreamResp:
    """Minimal stand-in for a streamed requests.Response (context manager)."""

    def __init__(self, content: bytes, headers: dict | None = None) -> None:
        self._content = content
        self.headers = headers or {}

    def __enter__(self) -> "_StreamResp":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 65536):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]


def _patch_get(monkeypatch, captured: list, resp: _StreamResp) -> None:
    import scraper.image_storage as image_storage

    def _get(url, timeout=15.0, stream=False):
        captured.append(url)
        return resp

    monkeypatch.setattr(image_storage.requests, "get", _get)


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
    _patch_get(monkeypatch, captured, _StreamResp(b"jpegbytes"))
    assert image_storage.download_image("https://d18-a.sdn.cz/x/y.jpeg") == b"jpegbytes"
    assert captured == ["https://d18-a.sdn.cz/x/y.jpeg?fl=res,1800,1800,1|shr,,20|jpg,80"]


def test_download_image_normalises_a_legacy_chain(monkeypatch):
    """The 868k-row legacy cohort carries the 749 CROP chain. Passing it through
    would keep re-downloading the cropped 4:3 frame, so it is REWRITTEN onto the
    master template before the request goes out."""
    import scraper.image_storage as image_storage

    captured: list[str] = []
    _patch_get(monkeypatch, captured, _StreamResp(b""))
    image_storage.download_image(
        "https://d18-a.sdn.cz/x/y.jpeg?fl=res,749,562,3|shr,,20|jpg,90"
    )
    assert captured == ["https://d18-a.sdn.cz/x/y.jpeg?fl=res,1800,1800,1|shr,,20|jpg,80"]


def test_download_image_honours_an_explicit_transform_ops(monkeypatch):
    """The re-master lane needs to request a template other than the default."""
    import scraper.image_storage as image_storage

    captured: list[str] = []
    _patch_get(monkeypatch, captured, _StreamResp(b""))
    image_storage.download_image(
        "https://d18-a.sdn.cz/x/y.jpeg", transform_ops="res,400,400,1|jpg,70"
    )
    assert captured == ["https://d18-a.sdn.cz/x/y.jpeg?fl=res,400,400,1|jpg,70"]


def test_download_image_rejects_oversize_content_length(monkeypatch):
    """A Content-Length over the cap is rejected before the body is read."""
    import scraper.image_storage as image_storage
    from scraper import media

    captured: list[str] = []
    headers = {"Content-Length": str(media.MAX_IMAGE_BYTES + 1)}
    _patch_get(monkeypatch, captured, _StreamResp(b"irrelevant", headers=headers))
    with pytest.raises(image_storage.NotAnImageError):
        image_storage.download_image("https://www.bazos.cz/img/1/1/1.jpg")


def test_download_image_rejects_oversize_body(monkeypatch):
    """A body that streams past the cap (no/short Content-Length) is rejected."""
    import scraper.image_storage as image_storage
    from scraper import media

    captured: list[str] = []
    oversize = b"\x00" * (media.MAX_IMAGE_BYTES + 1)
    _patch_get(monkeypatch, captured, _StreamResp(oversize))
    with pytest.raises(image_storage.NotAnImageError):
        image_storage.download_image("https://www.bazos.cz/img/1/1/1.jpg")


def test_with_transform_completes_rot_prefix_chain():
    """sreality ships some URLs with a prefix chain '?fl=rot,<deg>,0|' (trailing
    pipe). The CDN 400s it as-is AND with the pipe stripped; only the completed
    chain returns bytes. The rot op must be preserved — completing without it
    returns 200 but stores the photo unrotated (curl-verified)."""
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, with_transform

    url = "https://d18-a.sdn.cz/d_18/c_img_a/x.jpeg?fl=rot,180,0|"
    assert with_transform(url) == (
        "https://d18-a.sdn.cz/d_18/c_img_a/x.jpeg?fl=rot,180,0|" + IMAGE_TRANSFORM_OPS
    )


def test_with_transform_completes_rot_chain_without_trailing_pipe():
    """A rot prefix without the trailing pipe gets exactly one pipe separator."""
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, with_transform

    url = "https://d18-a.sdn.cz/d_18/c_img_a/x.jpeg?fl=rot,90,0"
    assert with_transform(url) == url + "|" + IMAGE_TRANSFORM_OPS


def test_with_transform_keeps_rot_when_normalising_a_legacy_chain():
    """A stored chain carrying BOTH a per-photo fact and the legacy serving ops:
    the rot survives in order, the crop ops are replaced."""
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, with_transform

    url = "https://d18-a.sdn.cz/x/y.jpeg?fl=rot,90,0|res,749,562,3|shr,,20|jpg,90"
    assert with_transform(url) == (
        "https://d18-a.sdn.cz/x/y.jpeg?fl=rot,90,0|" + IMAGE_TRANSFORM_OPS
    )


def test_with_transform_normalises_legacy_complete_chain():
    """THE inversion: the legacy 749 chain is mode-3 CROPPED (4:3), so passing it
    through would keep serving a cropped photo. It is rewritten, not preserved."""
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, with_transform

    legacy = "https://d18-a.sdn.cz/x/y.jpeg?fl=res,749,562,3|shr,,20|jpg,90"
    assert with_transform(legacy) == "https://d18-a.sdn.cz/x/y.jpeg?fl=" + IMAGE_TRANSFORM_OPS


def test_with_transform_is_idempotent():
    """Running it over its own output must be byte-identical — the URL is what the
    browser and the HTTP cache key on."""
    from scraper.image_storage import with_transform

    once = with_transform("https://d18-a.sdn.cz/x/y.jpeg")
    assert with_transform(once) == once
    rot = with_transform("https://d18-a.sdn.cz/x/y.jpeg?fl=rot,180,0|")
    assert with_transform(rot) == rot


def test_with_transform_never_percent_encodes_the_chain():
    """The CDN allowlist matches the template LITERALLY: %2C / %7C are rejected."""
    from scraper.image_storage import with_transform

    out = with_transform("https://d18-a.sdn.cz/x/y.jpeg?fl=rot,180,0|")
    assert "%2C" not in out and "%7C" not in out
    assert "," in out and "|" in out


def test_with_transform_keeps_other_query_params_and_fragment():
    from scraper.image_storage import IMAGE_TRANSFORM_OPS, with_transform

    url = "https://d18-a.sdn.cz/x/y.jpeg?v=3&fl=res,749,562,3#frag"
    assert with_transform(url) == (
        "https://d18-a.sdn.cz/x/y.jpeg?v=3&fl=" + IMAGE_TRANSFORM_OPS + "#frag"
    )


def test_rendition_for_splits_sreality_from_native():
    from scraper.image_storage import (
        RENDITION_NATIVE,
        RENDITION_SREALITY_MASTER,
        rendition_for,
    )

    assert rendition_for("https://d18-a.sdn.cz/x/y.jpeg") == RENDITION_SREALITY_MASTER
    assert rendition_for("https://www.bazos.cz/img/1/1/1.jpg") == RENDITION_NATIVE


def _tiny_jpeg(width: int = 7, height: int = 5) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), (1, 2, 3)).save(buf, format="JPEG")
    return buf.getvalue()


def test_image_dimensions_measures_real_bytes():
    pytest.importorskip("PIL", reason="Pillow is a scraper-runtime dependency")
    from scraper.image_storage import image_dimensions

    assert image_dimensions(_tiny_jpeg(7, 5)) == (7, 5)


def test_image_dimensions_returns_none_on_garbage():
    """Measuring must never fail a download — undecodable bytes store unmeasured."""
    from scraper.image_storage import image_dimensions

    assert image_dimensions(b"not an image at all") is None
    assert image_dimensions(b"") is None


def test_download_image_leaves_non_sreality_url_untouched(monkeypatch):
    """The render-transform is sreality-CDN-only; bazos (and other portals')
    image URLs must download verbatim (they 404 on the sreality query)."""
    import scraper.image_storage as image_storage

    captured: list[str] = []
    _patch_get(monkeypatch, captured, _StreamResp(b"bazosbytes"))
    bazos = "https://www.bazos.cz/img/1/123/456.jpg"
    assert image_storage.download_image(bazos) == b"bazosbytes"
    assert captured == [bazos]  # no transform appended
