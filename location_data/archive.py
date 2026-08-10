"""Archive every registry vintage's artefacts to R2 (04 §C1.8).

Archival is MANDATORY, not decoration: D4 binds every resolution to a named
`registry_version` and D2/D10 require resolutions to be re-runnable, but ČÚZK's CSV
directory is only ever verified to carry the CURRENT vintage — old vintages may vanish
when the next one is published. Without an archived copy a past `registry_version` becomes
unreproducible the moment ČÚZK rotates.

Design §C1.8 asks for a dedicated `ruian-archive` bucket; this ships into the platform's
EXISTING R2 bucket under the `backups/ruian-archive/` prefix instead — same account, same
credentials, one fewer piece of infrastructure to provision, and the key scheme keeps the
design's `<registry_version_label>/<original_filename>` shape. Objects are immutable: a key
that already holds the right byte count is left alone, never overwritten.

The manifest carries the licence text in force at fetch time, per §4.8's "licence posture
changes" scenario — CC BY 4.0 is irrevocable, so bytes obtained under it stay usable even
if ČÚZK later closes the data, but only if we recorded which licence they came under.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from typing import Any, Protocol

from location_data.ruian_csv import Artifact

LOG = logging.getLogger("location_data.archive")

PREFIX = "backups/ruian-archive"

# The licence in force at fetch time (04 §C4.4 / §4.10): VERIFIED for the address CSV
# series, ASSUMED for the SHP boundary packs (their own metadata record was never checked;
# the written default-if-unanswered posture is to treat them as CC BY 4.0).
LICENCE: dict[str, Any] = {
    "id": "CC-BY-4.0",
    "name": "Creative Commons Attribution 4.0 International",
    "url": "https://creativecommons.org/licenses/by/4.0/",
    "attribution": "© Český úřad zeměměřický a katastrální (ČÚZK), RÚIAN",
    "text": (
        "The data are published by ČÚZK as open data under the Creative Commons "
        "Attribution 4.0 International (CC BY 4.0) licence: free use, redistribution and "
        "derivative works are permitted provided the source is attributed. CC BY 4.0 is "
        "irrevocable, so bytes obtained under it remain usable under it."
    ),
    "verified_for": ["csv_ob_adr", "csv_strukt_adr"],
    "assumed_for": ["shp_stat"],
}


class ArchiveError(RuntimeError):
    """The vintage could not be archived — the reproducibility guarantee is unmet."""


class ObjectStore(Protocol):
    def upload_file(self, key: str, path: str, content_type: str = ...) -> None: ...
    def upload_bytes(self, key: str, data: bytes, content_type: str = ...) -> None: ...
    def object_size(self, key: str) -> int | None: ...


def artefact_key(version_label: str, filename: str) -> str:
    return f"{PREFIX}/{version_label}/{filename}"


def manifest_key(version_label: str) -> str:
    return f"{PREFIX}/{version_label}/manifest.json"


def build_manifest(
    version_label: str, artifacts: dict[str, Artifact], fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "registry_version_label": version_label,
        "archived_at": fetched_at or datetime.datetime.now(datetime.UTC).isoformat(),
        "licence": LICENCE,
        "artifacts": {
            name: {
                "url": a.url,
                "filename": a.path.name,
                "key": artefact_key(version_label, a.path.name),
                "bytes": a.bytes,
                "sha256": a.sha256,
                "etag": a.etag,
                "last_modified": a.last_modified,
            }
            for name, a in artifacts.items()
        },
    }


def open_store() -> ObjectStore:
    """The platform's R2 client, or ArchiveError naming the missing env vars."""
    from scraper import image_storage

    missing = [v for v in image_storage.R2_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise ArchiveError(
            "R2 is not configured, so the vintage cannot be archived and a past "
            f"registry_version would be unreproducible (04 §C1.8). Missing: {', '.join(missing)}. "
            "Pass --allow-unarchived to load anyway and state why in the run."
        )
    return image_storage.R2Client.from_env(max_pool_connections=4)


def archive_version(
    version_label: str,
    artifacts: dict[str, Artifact],
    *,
    store: ObjectStore | None = None,
) -> dict[str, str]:
    """Upload every artefact + the manifest; return the archive keys to record in
    `registry_versions.artifact_urls`. Raises ArchiveError on any failure — the caller
    aborts BEFORE staging, so an unarchived vintage never becomes `is_current`."""
    store = store or open_store()
    keys: dict[str, str] = {}
    try:
        for name, artifact in sorted(artifacts.items()):
            key = artefact_key(version_label, artifact.path.name)
            existing = store.object_size(key)
            if existing == artifact.bytes:
                LOG.info("ARCHIVE already present key=%s bytes=%d", key, existing)
            else:
                if existing is not None:
                    raise ArchiveError(
                        f"{key} already exists with {existing} bytes, not {artifact.bytes} — "
                        "archived vintages are immutable; refusing to overwrite"
                    )
                store.upload_file(key, str(artifact.path), "application/zip")
                LOG.info("ARCHIVE uploaded key=%s bytes=%d", key, artifact.bytes)
            keys[f"{name}_archive"] = key
        manifest = build_manifest(version_label, artifacts)
        store.upload_bytes(
            manifest_key(version_label),
            json.dumps(manifest, indent=2, sort_keys=True).encode(),
            "application/json",
        )
        keys["manifest_archive"] = manifest_key(version_label)
    except ArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001 — any R2 failure is an archive failure
        raise ArchiveError(f"archiving {version_label} failed: {exc}") from exc
    return keys
