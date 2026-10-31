"""Shared fixtures for the location-data loaders. No network, no database."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "location_data"

_KEYS = ("ruian_adm_kod", "ulice_kod", "cast_obce_kod", "obec_kod", "okres_kod", "kraj_kod")


@pytest.fixture(autouse=True)
def _grain_is_the_finest_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """W5's property rail over EVERY resolution a test here makes: the published grain is
    `grain_of(keys)`, so no second grain writer can come back."""
    from location_data.resolver import core, grade
    from location_data.resolver.types import Fill, Verdict

    def checked(*args, resolve=core.resolve, **kwargs):
        row = resolve(*args, **kwargs)
        keys = Fill(**{key: getattr(row, key) for key in _KEYS})
        assert row.granularity == grade.grain_of(keys, Verdict(row.country_status, row.country_code))
        return row

    monkeypatch.setattr(core, "resolve", checked)


@pytest.fixture
def ob_adr_zip(tmp_path: Path) -> Path:
    """The OB_ADR product shape: a zip of per-obec CSVs, CP1250."""
    path = tmp_path / "20260731_OB_ADR_csv.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("20260731_OB_554782_ADR.csv", (FIXTURES / "ob_adr_sample.csv").read_bytes())
    return path


@pytest.fixture
def strukt_zip(tmp_path: Path) -> Path:
    """The strukt_ADR product shape: 7 CSVs under strukturovane-CSV/."""
    path = tmp_path / "20260731_strukt_ADR.csv.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for member in sorted((FIXTURES / "strukturovane-CSV").glob("*.csv")):
            zf.writestr(f"strukturovane-CSV/{member.name}", member.read_bytes())
    return path
