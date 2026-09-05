"""Mirror externally-licensed model weights into R2 with a recorded checksum.

Why this exists: the DINOv3 weights come from Meta behind a licence acceptance — either the
Hugging Face gated repo (manual approval, then a token) or Meta's download e-mail (time-limited
signed URLs). Neither may be a runtime dependency: a pod cannot click through a licence and a
signed link expires. So the accepted files are copied ONCE into our own bucket, hashed, and every
later job reads from there. The signed URL is a credential — it is never written to the manifest
or the logs; only the host and the file name are.

Usage (GitHub Actions lane `mirror_model_weights.yml`, or locally with R2_* + the env var set):
    MODEL_WEIGHT_URLS="https://.../dinov3_vitb16_pretrain_lvd1689m-xxxx.pth" \
        python -m scripts.mirror_model_weights --prefix models/dinov3 [--apply] [--note "..."]

Without --apply nothing is uploaded: files are downloaded, hashed and reported (a dry run).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests

log = logging.getLogger("mirror_model_weights")

URLS_ENV = "MODEL_WEIGHT_URLS"
DEFAULT_PREFIX = "models/dinov3"
CHUNK = 8 * 1024 * 1024
TIMEOUT = (30, 300)


def parse_urls(text: str | None) -> list[str]:
    """One URL per line (commas also accepted); blanks and `#` comments ignored."""
    if not text:
        return []
    out: list[str] = []
    for raw in text.replace(",", "\n").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not s.startswith(("http://", "https://")):
            raise ValueError(f"not an http(s) URL: {s[:40]!r}")
        out.append(s)
    return out


def object_name(url: str) -> str:
    """The file name = last path segment, query string dropped (signatures live there)."""
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1].strip()
    if not name:
        raise ValueError("URL has no file name in its path")
    return name


def source_host(url: str) -> str:
    return urlparse(url).netloc


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: str, session: requests.Session | None = None) -> int:
    """Stream one URL to `dest`; returns the byte count."""
    sess = session or requests.Session()
    size = 0
    with sess.get(url, stream=True, timeout=TIMEOUT) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for block in resp.iter_content(chunk_size=CHUNK):
                if block:
                    fh.write(block)
                    size += len(block)
    return size


def manifest_key(prefix: str) -> str:
    return f"{prefix.strip('/')}/MANIFEST.json"


def make_entry(
    *, filename: str, key: str, sha256: str, size: int, host: str, note: str, fetched_at: str
) -> dict[str, Any]:
    return {
        "filename": filename,
        "key": key,
        "sha256": sha256,
        "size_bytes": size,
        "source_host": host,
        "fetched_at": fetched_at,
        "note": note,
    }


def merge_manifest(existing: dict[str, Any] | None, entry: dict[str, Any]) -> dict[str, Any]:
    """Manifest = {"files": {filename: entry}}; a re-mirror of the same file replaces its row."""
    doc: dict[str, Any] = dict(existing or {})
    files = dict(doc.get("files") or {})
    files[entry["filename"]] = entry
    doc["files"] = files
    doc["updated_at"] = entry["fetched_at"]
    return doc


def _mask_for_actions(urls: list[str]) -> None:
    if os.environ.get("GITHUB_ACTIONS"):
        for u in urls:
            print(f"::add-mask::{u}", flush=True)


def _load_manifest(r2: Any, key: str) -> dict[str, Any] | None:
    try:
        raw = r2.download_bytes(key)
    except Exception:  # noqa: BLE001 — a missing manifest is the normal first-run case
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("existing manifest at %s is not valid JSON — starting a fresh one", key)
        return None


def run(
    urls: list[str],
    prefix: str,
    *,
    apply: bool,
    note: str,
    r2: Any = None,
    session: requests.Session | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Download + hash every URL; upload and update the manifest only when `apply`."""
    if not urls:
        raise SystemExit(f"no URLs — set {URLS_ENV} (one per line)")
    if apply and r2 is None:
        raise SystemExit("--apply needs an R2 client (R2_* env vars)")
    fetched_at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    prefix = prefix.strip("/")
    entries: list[dict[str, Any]] = []
    manifest = _load_manifest(r2, manifest_key(prefix)) if apply else None
    with tempfile.TemporaryDirectory() as tmp:
        for url in urls:
            name = object_name(url)
            key = f"{prefix}/{name}"
            dest = os.path.join(tmp, name)
            log.info("downloading %s from %s", name, source_host(url))
            size = download(url, dest, session=session)
            digest = sha256_of(dest)
            entry = make_entry(
                filename=name, key=key, sha256=digest, size=size, host=source_host(url),
                note=note, fetched_at=fetched_at,
            )
            entries.append(entry)
            log.info("  %s  %d bytes  sha256=%s", name, size, digest)
            if not apply:
                log.info("  dry run — not uploaded (pass --apply)")
                continue
            existing = r2.object_size(key)
            if existing == size:
                log.info("  already in R2 at %s with the same size — upload skipped", key)
            else:
                r2.upload_file(key, dest, content_type="application/octet-stream")
                log.info("  uploaded to %s", key)
            manifest = merge_manifest(manifest, entry)
            os.remove(dest)
    if apply and manifest is not None:
        r2.upload_bytes(
            manifest_key(prefix),
            json.dumps(manifest, indent=1, sort_keys=True).encode("utf-8"),
            content_type="application/json",
        )
        log.info("manifest written to %s", manifest_key(prefix))
    return entries


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="R2 key prefix (default models/dinov3)")
    ap.add_argument("--apply", action="store_true", help="Upload + write the manifest (default: dry run)")
    ap.add_argument("--note", default="", help="Free text stored in the manifest, e.g. who accepted the licence and when")
    args = ap.parse_args(argv)
    urls = parse_urls(os.environ.get(URLS_ENV))
    _mask_for_actions(urls)
    r2 = None
    if args.apply:
        from scraper.image_storage import R2Client

        r2 = R2Client.from_env()
    entries = run(urls, args.prefix, apply=args.apply, note=args.note, r2=r2)
    print(json.dumps([{k: v for k, v in e.items()} for e in entries], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
