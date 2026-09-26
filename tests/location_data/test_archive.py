"""Vintage archival (04 §C1.8): keys, manifest, the restore a resume reads, and the abort
contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from location_data import archive
from location_data.ruian_csv import Artifact


class _Store:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.uploads: list[tuple[str, str]] = []
        self.objects: dict[str, bytes] = dict(objects or {})

    def upload_file(self, key: str, path: str, content_type: str = "application/zip") -> None:
        self.uploads.append((key, path))
        self.objects[key] = Path(path).read_bytes()

    def upload_bytes(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        self.objects[key] = data

    def download_file(self, key: str, path: str) -> None:
        Path(path).write_bytes(self.objects[key])


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


def test_an_orphan_left_by_a_run_that_never_recorded_its_version_is_replaced(tmp_path: Path):
    """Archiving runs only for a vintage no version row records, so an object already under
    the key is unreferenced — the next run's bytes, which it then records, replace it."""
    artifact = _artifact(tmp_path, "OB_ADR_csv", b"today")
    key = archive.artefact_key("ruian:2026-07-31", artifact.path.name)
    store = _Store({key: b"yesterday"})
    archive.archive_version("ruian:2026-07-31", {"csv_ob_adr": artifact}, store=store)
    assert store.objects[key] == b"today"


def test_restore_downloads_each_artefact_under_its_archived_filename(tmp_path: Path):
    key = archive.artefact_key("ruian:2026-09-30", "ruian_shp_stat.zip")
    store = _Store({key: b"pack"})
    paths = archive.restore({"shp_stat": key}, tmp_path, store=store)
    assert paths == {"shp_stat": tmp_path / "ruian_shp_stat.zip"}
    assert paths["shp_stat"].read_bytes() == b"pack"
    with pytest.raises(archive.ArchiveError):
        archive.restore({"shp_stat": key + ".gone"}, tmp_path, store=store)


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
    assert "never proceeds unarchived" in str(exc.value)
