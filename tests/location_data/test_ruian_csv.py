"""Parser + download contract for the two RÚIAN CSV products. No network."""

from __future__ import annotations

import datetime
import hashlib
import zipfile
from pathlib import Path

import pytest

from location_data import ruian_csv
from location_data.krovak import KrovakPositive


def test_address_rows_preserve_all_nineteen_columns(ob_adr_zip: Path):
    rows = list(ruian_csv.iter_address_points(ob_adr_zip))
    assert [r.kod_adm for r in rows] == [21690278, 99000001, 99000002, 99000003]

    castle = rows[0]
    assert castle.obec_kod == 554782
    assert castle.obec_nazev == "Praha"
    assert castle.momc_kod == 500054
    assert castle.momc_nazev == "Praha 1"
    assert castle.op_kod == 19
    assert castle.cast_obce_kod == 490075
    assert castle.cast_obce_nazev == "Hradčany"
    assert castle.ulice_kod == 482536
    assert castle.ulice_nazev == "Hrad I. nádvoří"
    assert castle.typ_so == "č.p."
    assert castle.cislo_domovni == 1
    assert castle.cislo_orientacni is None
    assert castle.psc == "11900"
    assert castle.krovak == KrovakPositive(744384.54, 1042569.73)
    assert castle.plati_od == datetime.date(2017, 6, 7)


def test_street_less_row_without_coordinates(ob_adr_zip: Path):
    row = next(r for r in ruian_csv.iter_address_points(ob_adr_zip) if r.kod_adm == 99000002)
    assert row.ulice_kod is None and row.ulice_nazev is None
    assert row.krovak is None
    assert row.typ_so == "č.ev."
    assert row.psc == "12345"  # PSČ survives even without a street


def test_orientation_number_and_letter_stay_separate(ob_adr_zip: Path):
    row = next(r for r in ruian_csv.iter_address_points(ob_adr_zip) if r.kod_adm == 99000001)
    assert (row.cislo_domovni, row.cislo_orientacni, row.znak_orientacniho) == (12, 3, "a")


def test_out_of_envelope_ordinates_are_reported_not_fatal(ob_adr_zip: Path):
    seen: list[tuple[int, str, str]] = []
    rows = list(ruian_csv.iter_address_points(
        ob_adr_zip, on_bad_coordinate=lambda k, y, x: seen.append((k, y, x))
    ))
    assert [k for k, _, _ in seen] == [99000003]
    bad = next(r for r in rows if r.kod_adm == 99000003)
    assert bad.krovak is None
    assert bad.psc == "12345"


def test_header_drift_is_loud(tmp_path: Path):
    path = tmp_path / "drift.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("x_ADR.csv", "Kód ADM;Kód obce\r\n1;2\r\n".encode("cp1250"))
    with pytest.raises(ruian_csv.RuianSchemaError):
        list(ruian_csv.iter_address_points(path))


def test_strukt_members_parse_with_blanks_preserved(strukt_zip: Path):
    chain = list(ruian_csv.iter_strukt(strukt_zip, "chain"))
    assert len(chain) == 4
    praha = chain[0]
    assert praha[0] == "21690278"
    assert praha[9] == ""  # Praha genuinely has no okres in RÚIAN's own chain
    assert praha[6] == "554782"

    cast_obce = list(ruian_csv.iter_strukt(strukt_zip, "cast_obce"))
    assert [r[0] for r in cast_obce] == ["490075", "195901"]
    assert list(ruian_csv.iter_strukt(strukt_zip, "ulice")) == [
        ["482536", "554782"], ["123456", "500011"]
    ]
    assert list(ruian_csv.iter_strukt(strukt_zip, "katastr")) == [["67067", "667064", "500011"]]
    assert len(list(ruian_csv.iter_strukt(strukt_zip, "momc_praha"))) == 1


def test_strukt_missing_member_is_loud(tmp_path: Path):
    path = tmp_path / "empty.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("readme.txt", "nothing here")
    with pytest.raises(ruian_csv.RuianSchemaError):
        list(ruian_csv.iter_strukt(path, "chain"))


def test_candidate_vintages_are_month_ends_newest_first():
    got = ruian_csv.candidate_vintages(datetime.date(2026, 8, 10), 3)
    assert got == [
        datetime.date(2026, 7, 31), datetime.date(2026, 6, 30), datetime.date(2026, 5, 31)
    ]


def test_candidate_vintages_on_the_first_offer_that_months_vintage_first():
    # The monthly job runs on the 1st at 10:00, after the measured 08:04 generation of
    # OB_ADR — so the vintage dated the last day of the previous month is candidate #1.
    got = ruian_csv.candidate_vintages(datetime.date(2026, 8, 1), 2)
    assert got == [datetime.date(2026, 7, 31), datetime.date(2026, 6, 30)]


class _FakeResponse:
    def __init__(self, status: int = 200, headers: dict | None = None, body: bytes = b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected raise_for_status at {self.status_code}")

    def iter_content(self, size: int):
        yield self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, available: set[str], body: bytes = b"payload"):
        self.available = available
        self.body = body
        self.head_calls: list[str] = []

    def head(self, url: str, **_kwargs) -> _FakeResponse:
        self.head_calls.append(url)
        if url not in self.available:
            return _FakeResponse(404)
        return _FakeResponse(200, {"content-length": "10", "etag": '"abc"',
                                   "last-modified": "Fri, 31 Jul 2026 23:43:33 GMT"})

    def get(self, url: str, **_kwargs) -> _FakeResponse:
        return _FakeResponse(200, {"etag": '"abc"', "last-modified": "x"}, self.body)


def test_discover_vintage_takes_the_newest_published(tmp_path: Path):
    url = ruian_csv.STRUKT_URL.format(vintage="20260630")
    sess = _FakeSession({url})
    got = ruian_csv.discover_vintage(sess, today=datetime.date(2026, 8, 10))
    assert got == datetime.date(2026, 6, 30)
    assert sess.head_calls[0].endswith("20260731_strukt_ADR.csv.zip")


def test_discover_vintage_none_when_nothing_published():
    assert ruian_csv.discover_vintage(_FakeSession(set()), today=datetime.date(2026, 8, 10)) is None


def test_download_records_bytes_and_sha256(tmp_path: Path):
    sess = _FakeSession(set(), body=b"hello ruian")
    artifact = ruian_csv.download(sess, "csv_ob_adr", "https://example/x.zip", tmp_path / "x.zip")
    assert artifact.bytes == len(b"hello ruian")
    assert artifact.sha256 == hashlib.sha256(b"hello ruian").hexdigest()
    assert artifact.path.read_bytes() == b"hello ruian"
    assert artifact.etag == '"abc"'
