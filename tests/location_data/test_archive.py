"""Vintage archival (04 §C1.8): keys, manifest, immutability, and the abort contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from location_data import archive
from location_data.ruian_csv import Artifact


class _Store:
    def __init__(self, existing: dict[str, int] | None = None):
        self.existing = dict(existing or {})
        self.uploads: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = {}

    def object_size(self, key: str) -> int | None:
        return self.existing.get(key)

    def upload_file(self, key: str, path: str, content_type: str = "application/zip") -> None:
        self.uploads.append((key, path))
        self.existing[key] = Path(path).stat().st_size

    def upload_bytes(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        self.objects[key] = data


def _artifact(tmp_path: Path, name: str, body: bytes = b"payload") -> Artifact:
    path = tmp_path / f"20260731_{name}.zip"
    path.write_bytes(body)
    return Artifact(name=name, url=f"https://vdp.cuzk.gov.cz/{path.name}", path=path,
                    bytes=len(body), sha256="a" * 64, etag='"e"', last_modified="lm")


def test_keys_keep_the_designs_label_slash_filename_shape(tmp_path: Path):
    key = archive.artefact_key("ruian:2026-07-31", "20260731_OB_ADR_csv.zip")
    assert key == "backups/ruian-archive/ruian:2026-07-31/20260731_OB_ADR_csv.zip"
    assert archive.manifest_key("ruian:2026-07-31").endswith("/manifest.json")


def test_archive_uploads_every_artefact_and_a_manifest(tmp_path: Path):
    artifacts = {"csv_ob_adr": _artifact(tmp_path, "OB_ADR_csv"),
                 "csv_strukt_adr": _artifact(tmp_path, "strukt_ADR")}
    store = _Store()
    keys = archive.archive_version("ruian:2026-07-31", artifacts, store=store)
    assert len(store.uploads) == 2
    assert set(keys) == {"csv_ob_adr_archive", "csv_strukt_adr_archive", "manifest_archive"}
    manifest = json.loads(store.objects[archive.manifest_key("ruian:2026-07-31")])
    assert set(manifest["artifacts"]) == {"csv_ob_adr", "csv_strukt_adr"}
    assert manifest["artifacts"]["csv_ob_adr"]["sha256"] == "a" * 64


def test_the_manifest_carries_the_licence_text_in_force(tmp_path: Path):
    """CC BY 4.0 is irrevocable — archived bytes stay usable if ČÚZK later closes the
    data, but only if we recorded which licence they were obtained under (§4.8)."""
    store = _Store()
    archive.archive_version("ruian:2026-07-31", {"a": _artifact(tmp_path, "a")}, store=store)
    manifest = json.loads(store.objects[archive.manifest_key("ruian:2026-07-31")])
    assert manifest["licence"]["id"] == "CC-BY-4.0"
    assert "Creative Commons" in manifest["licence"]["text"]
    assert manifest["licence"]["attribution"]


def test_an_already_archived_artefact_is_not_re_uploaded(tmp_path: Path):
    artifact = _artifact(tmp_path, "OB_ADR_csv")
    key = archive.artefact_key("ruian:2026-07-31", artifact.path.name)
    store = _Store({key: artifact.bytes})
    archive.archive_version("ruian:2026-07-31", {"csv_ob_adr": artifact}, store=store)
    assert store.uploads == []


def test_a_key_holding_different_bytes_is_never_overwritten(tmp_path: Path):
    artifact = _artifact(tmp_path, "OB_ADR_csv")
    key = archive.artefact_key("ruian:2026-07-31", artifact.path.name)
    store = _Store({key: artifact.bytes + 1})
    with pytest.raises(archive.ArchiveError):
        archive.archive_version("ruian:2026-07-31", {"csv_ob_adr": artifact}, store=store)
    assert store.uploads == []


def test_any_store_failure_becomes_an_archive_error(tmp_path: Path):
    class _Broken(_Store):
        def upload_file(self, key, path, content_type="application/zip"):
            raise RuntimeError("R2 503")

    with pytest.raises(archive.ArchiveError):
        archive.archive_version(
            "ruian:2026-07-31", {"a": _artifact(tmp_path, "a")}, store=_Broken()
        )


def test_missing_r2_credentials_name_the_env_vars(monkeypatch):
    for var in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(archive.ArchiveError) as exc:
        archive.open_store()
    assert "R2_ACCOUNT_ID" in str(exc.value)
    assert "--allow-unarchived" in str(exc.value)
