"""`GET /buildings/{id}/attachments/{aid}/raw` — the attachment byte proxy's cache policy.

The route reads bytes out of R2 and streams them through Railway, and the frontend renders
previews by `fetch`-ing it into a blob URL (an `<img>` can't send a bearer header). With no
`Cache-Control` on the response every `AttachmentCard` mount re-pulled the whole file — up
to 25 MB — over two hops. The bytes are immutable (the bucket key carries a per-upload
uuid), so they are cacheable; `private` because they are operator-only uploads behind a
bearer token and must never be stored by a shared cache.
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
TestClient = pytest.importorskip("fastapi.testclient").TestClient

from api import attachments as attachments_module
from api import dependencies as deps
from api import main as api_main


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(
        attachments_module,
        "fetch_attachment",
        lambda conn, aid: {"id": aid, "building_run_id": 7},
    )
    monkeypatch.setattr(
        attachments_module,
        "download_attachment_bytes",
        lambda conn, aid: (b"\xff\xd8\xff-jpeg-bytes", "image/jpeg", "plan.jpg"),
    )
    api_main.app.dependency_overrides[deps.get_db_conn] = lambda: object()
    api_main.app.dependency_overrides[deps.require_token] = lambda: None
    yield TestClient(api_main.app)
    api_main.app.dependency_overrides.clear()


def _cache_control(res: Any) -> str:
    return res.headers.get("cache-control", "")


def test_raw_attachment_is_cacheable(client):
    res = client.get("/buildings/7/attachments/3/raw")
    assert res.status_code == 200
    assert res.content == b"\xff\xd8\xff-jpeg-bytes"
    assert "max-age=" in _cache_control(res)


def test_raw_attachment_is_never_publicly_cacheable(client):
    """Operator-private bytes behind a bearer token — a shared cache must not store them."""
    cc = _cache_control(client.get("/buildings/7/attachments/3/raw"))
    assert "private" in cc
    assert "public" not in cc


def test_raw_attachment_still_404s_for_a_foreign_building(client):
    """Caching must not have widened what the route will serve."""
    assert client.get("/buildings/8/attachments/3/raw").status_code == 404
