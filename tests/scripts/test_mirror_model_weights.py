"""Hermetic tests for scripts/mirror_model_weights.py — no network, no R2."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

from scripts import mirror_model_weights as m

SIGNED = "https://dl.example.net/dinov3/dinov3_vitb16_pretrain_lvd1689m-73cec8be.pth?Signature=abc&Expires=1"


def test_parse_urls_accepts_lines_commas_comments_and_rejects_non_http():
    text = "# from the e-mail\n" + SIGNED + "\n\nhttps://x.y/a.pth, https://x.y/b.pth\n"
    assert m.parse_urls(text) == [SIGNED, "https://x.y/a.pth", "https://x.y/b.pth"]
    assert m.parse_urls(None) == []
    with pytest.raises(ValueError):
        m.parse_urls("ftp://x.y/a.pth")


def test_object_name_drops_query_and_refuses_empty():
    assert m.object_name(SIGNED) == "dinov3_vitb16_pretrain_lvd1689m-73cec8be.pth"
    with pytest.raises(ValueError):
        m.object_name("https://dl.example.net/dinov3/")


def test_manifest_merge_replaces_same_file_and_never_stores_the_url():
    e1 = m.make_entry(filename="a.pth", key="models/dinov3/a.pth", sha256="1", size=1,
                      host="dl.example.net", note="n", fetched_at="t1")
    e2 = dict(e1, sha256="2", fetched_at="t2")
    doc = m.merge_manifest(None, e1)
    doc = m.merge_manifest(doc, e2)
    assert doc["files"]["a.pth"]["sha256"] == "2"
    assert doc["updated_at"] == "t2"
    assert "Signature" not in json.dumps(doc) and "?" not in json.dumps(doc)


class _Resp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        yield self._payload[: len(self._payload) // 2]
        yield self._payload[len(self._payload) // 2 :]


class _Session:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.seen: list[str] = []

    def get(self, url, stream=True, timeout=None):
        self.seen.append(url)
        return _Resp(self.payload)


class _FakeR2:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploaded_files: list[tuple[str, str]] = []

    def download_bytes(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]

    def object_size(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def upload_file(self, key, path, content_type="application/zip"):
        with open(path, "rb") as fh:
            self.objects[key] = fh.read()
        self.uploaded_files.append((key, content_type))

    def upload_bytes(self, key, data, content_type="image/jpeg"):
        self.objects[key] = data


def test_dry_run_downloads_hashes_and_uploads_nothing():
    payload = b"weights" * 1000
    sess = _Session(payload)
    entries = m.run([SIGNED], "models/dinov3", apply=False, note="", session=sess,
                    now=datetime(2026, 9, 5, tzinfo=timezone.utc))
    assert sess.seen == [SIGNED]
    assert entries[0]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert entries[0]["size_bytes"] == len(payload)
    assert entries[0]["source_host"] == "dl.example.net"
    assert "Signature" not in json.dumps(entries)


def test_apply_uploads_writes_manifest_and_skips_an_identical_rerun():
    payload = b"weights" * 1000
    r2 = _FakeR2()
    sess = _Session(payload)
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    m.run([SIGNED], "models/dinov3/", apply=True, note="accepted by operator", r2=r2,
          session=sess, now=now)
    key = "models/dinov3/dinov3_vitb16_pretrain_lvd1689m-73cec8be.pth"
    assert r2.objects[key] == payload
    manifest = json.loads(r2.objects["models/dinov3/MANIFEST.json"])
    row = manifest["files"]["dinov3_vitb16_pretrain_lvd1689m-73cec8be.pth"]
    assert row["sha256"] == hashlib.sha256(payload).hexdigest()
    assert row["note"] == "accepted by operator"
    assert len(r2.uploaded_files) == 1
    m.run([SIGNED], "models/dinov3", apply=True, note="", r2=r2, session=sess, now=now)
    assert len(r2.uploaded_files) == 1  # same size already there → skipped


def test_apply_without_r2_and_empty_urls_refuse():
    with pytest.raises(SystemExit):
        m.run([], "models/dinov3", apply=False, note="")
    with pytest.raises(SystemExit):
        m.run([SIGNED], "models/dinov3", apply=True, note="", r2=None)
