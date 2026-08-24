"""The anchored presign — a stable redirect target for the /images cache key.

`GET /images/{key}` re-mints its 302 hourly on purpose (a credential rotation has to
self-heal), but SigV4 signs off the wall clock, so every re-mint used to produce a
different URL for identical bytes. The browser caches image bytes under the full URL,
so R2's own `max-age=2592000` never applied twice and every photo re-downloaded hourly.

The fix pins the signing timestamp to the start of a bucket. These tests hold two things
that matter: that the anchored signer is byte-for-byte the SAME signature boto3 itself
would produce for that instant (so pinning the clock is the only behaviour changed), and
that the anchor stays far enough below the presign TTL that the last URL of a bucket is
still valid for days after the bucket rolls.
"""

from __future__ import annotations

import pytest

pytest.importorskip("boto3")
pytest.importorskip("botocore")

import botocore.auth

from scraper import image_storage

_ACCOUNT = "acct123"
_BUCKET = "sreality-images"
_KEY = "-53026/0022.jpg"
_TTL = 604800
_DAY = 86400


def _client() -> image_storage.R2Client:
    return image_storage.R2Client(
        account_id=_ACCOUNT,
        access_key_id="AKIAEXAMPLE",
        secret_access_key="secretexample",
        bucket=_BUCKET,
    )


def _query(url: str) -> dict[str, str]:
    from urllib.parse import parse_qsl, urlsplit

    return dict(parse_qsl(urlsplit(url).query))


def test_anchored_url_is_identical_across_remints():
    """The whole point: two mints seconds apart must be the same string."""
    client = _client()
    first = client.presigned_get(_KEY, expires_in=_TTL, anchor_seconds=_DAY)
    second = client.presigned_get(_KEY, expires_in=_TTL, anchor_seconds=_DAY)
    assert first == second


def test_unanchored_url_changes_between_remints(monkeypatch):
    """The bug, pinned: without an anchor the signature moves with the clock."""
    client = _client()
    stamps = iter(["20260824T120000Z", "20260824T130000Z"])
    real = botocore.auth.SigV4Auth._modify_request_before_signing

    def _stamped(self, request):
        request.context["timestamp"] = next(stamps)
        return real(self, request)

    monkeypatch.setattr(
        botocore.auth.SigV4QueryAuth, "_modify_request_before_signing", _stamped
    )
    assert client.presigned_get(_KEY, expires_in=_TTL) != client.presigned_get(
        _KEY, expires_in=_TTL
    )


def test_anchored_signature_matches_boto3_at_the_same_instant(monkeypatch):
    """Equivalence with the reference implementation.

    Freeze botocore's clock to the anchor instant and ask boto3 for an ordinary presigned
    URL; the anchored signer must return that exact string. This is what makes it safe to
    sign through a subclass rather than reimplementing SigV4 — only the timestamp differs
    from what boto3 does, and here it doesn't even differ.
    """
    import datetime

    anchor_epoch = 1_787_536_000 // _DAY * _DAY
    frozen = datetime.datetime.fromtimestamp(anchor_epoch, datetime.timezone.utc)
    monkeypatch.setattr(botocore.auth, "get_current_datetime", lambda: frozen)
    monkeypatch.setattr(image_storage.time, "time", lambda: anchor_epoch + 3607)

    client = _client()
    reference = client.presigned_get(_KEY, expires_in=_TTL)
    anchored = client.presigned_get(_KEY, expires_in=_TTL, anchor_seconds=_DAY)
    assert anchored == reference
    assert _query(anchored)["X-Amz-Date"] == frozen.strftime("%Y%m%dT%H%M%SZ")


def test_anchor_stamp_floors_to_the_bucket_start():
    midnight = 1_787_529_600  # 2026-08-24T00:00:00Z
    assert image_storage.presign_anchor_stamp(_DAY, midnight) == "20260824T000000Z"
    # Every instant inside the bucket resolves to the same stamp, including its last second.
    for offset in (1, 3_600, 43_200, _DAY - 1):
        assert (
            image_storage.presign_anchor_stamp(_DAY, midnight + offset)
            == "20260824T000000Z"
        )
    # The next bucket rolls over exactly once, not before.
    assert image_storage.presign_anchor_stamp(_DAY, midnight + _DAY) == "20260825T000000Z"


def test_anchored_url_keeps_the_signed_shape_boto3_produces():
    client = _client()
    params = _query(client.presigned_get(_KEY, expires_in=_TTL, anchor_seconds=_DAY))
    assert params["X-Amz-Algorithm"] == "AWS4-HMAC-SHA256"
    assert params["X-Amz-Expires"] == str(_TTL)
    assert params["X-Amz-SignedHeaders"] == "host"
    # R2's credential scope: <key>/<date>/auto/s3/aws4_request.
    assert params["X-Amz-Credential"].endswith("/auto/s3/aws4_request")
    assert client.presigned_get(_KEY, expires_in=_TTL, anchor_seconds=_DAY).startswith(
        f"https://{_ACCOUNT}.r2.cloudflarestorage.com/{_BUCKET}/{_KEY}?"
    )


def test_zero_anchor_falls_back_to_per_request_signing():
    """The kill switch has to reach the signer, not just the route."""
    client = _client()
    assert "X-Amz-Signature" in _query(
        client.presigned_get(_KEY, expires_in=_TTL, anchor_seconds=0)
    )


def test_anchor_leaves_days_of_validity_after_the_bucket_rolls():
    """The invariant the anchor width has to respect.

    A URL minted at the very end of a bucket was signed at the bucket's START, so it has
    TTL - anchor left. Let those meet and the last request of a bucket would be handed an
    already-expired URL.
    """
    from api.routes import images as images_route

    assert images_route._PRESIGN_ANCHOR_DEFAULT <= images_route._PRESIGN_TTL // 2
    remaining = images_route._PRESIGN_TTL - images_route._PRESIGN_ANCHOR_DEFAULT
    assert remaining >= 3 * _DAY
