"""Download + parse the ČÚZK RÚIAN national address CSV products (C1).

Two products compose one baseline (04 C1.2 / 01 §3.4.1) and neither alone is sufficient:

    OB_ADR_csv    zip of 6,258 per-obec CSVs — the 19 attribute columns, incl. PSČ and the
                  positive-Křovák ordinates.
    strukt_ADR    national structured set (7 files) — the admin chain pre-joined, codes only.

Encoding is CP1250, delimiter ';'. Vintages are the last day of the previous month and are
HEAD-probed, never computed (04 C1.3). Every artefact carries its byte count, sha256, etag
and last-modified so the measured ~8 h 21 m generation skew between the two products stays
reconstructable.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import io
import logging
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import requests

from location_data.krovak import KrovakPositive, KrovakSignError

LOG = logging.getLogger("location_data.ruian_csv")

CSV_BASE = "https://vdp.cuzk.gov.cz/vymenny_format/csv"
OB_ADR_URL = CSV_BASE + "/{vintage}_OB_ADR_csv.zip"
STRUKT_URL = CSV_BASE + "/{vintage}_strukt_ADR.csv.zip"
OBEC_ADR_URL = CSV_BASE + "/{vintage}_OB_{obec}_ADR.csv.zip"

USER_AGENT = "sreality-location-data/1 (+ruian registry mirror)"
_TIMEOUT = (30, 600)
_CHUNK = 1 << 20

ADR_HEADER = (
    "Kód ADM;Kód obce;Název obce;Kód MOMC;Název MOMC;Kód obvodu Prahy;Název obvodu Prahy;"
    "Kód části obce;Název části obce;Kód ulice;Název ulice;Typ SO;Číslo domovní;"
    "Číslo orientační;Znak čísla orientačního;PSČ;Souřadnice Y;Souřadnice X;Platí Od"
).split(";")

STRUKT_MEMBERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "chain": (
        "strukturovane-CSV/adresni-mista-vazby-cr.csv",
        ("ADM_KOD", "ULICE_KOD", "COBCE_KOD", "MOMC_KOD", "OP_KOD", "SPRAVOBV_KOD",
         "OBEC_KOD", "POU_KOD", "ORP_KOD", "OKRES_KOD", "VUSC_KOD", "VO_KOD"),
    ),
    "cast_obce": (
        "strukturovane-CSV/vazby-cr.csv",
        ("COBCE_KOD", "OBEC_KOD", "POU_KOD", "ORP_KOD", "OKRES_KOD", "VUSC_KOD",
         "REGSOUDR_KOD", "STAT_KOD"),
    ),
    "ulice": (
        "strukturovane-CSV/vazby-ulice-obce-s-ulicni-siti.csv",
        ("ULICE_KOD", "OBEC_KOD"),
    ),
    "katastr": (
        "strukturovane-CSV/vazby-katastr-uzemi-cr.csv",
        ("ZSJ_KOD", "KATUZ_KOD", "OBEC_KOD"),
    ),
    "pou": (
        "strukturovane-CSV/vazby-orp-cr.csv",
        ("POU_KOD", "ORP_KOD", "OKRES_KOD", "VUSC_KOD", "REGSOUDR_KOD", "STAT_KOD"),
    ),
    "momc_statutarni": (
        "strukturovane-CSV/vazby-momc-statutarni-mesta.csv",
        ("MOMC_KOD", "OP_KOD", "OBEC_KOD", "POU_KOD", "ORP_KOD", "VUSC_KOD",
         "REGSOUDR_KOD", "STAT_KOD"),
    ),
    "momc_praha": (
        "strukturovane-CSV/vazby-hlm-praha.csv",
        ("MOMC_KOD", "SPRAVOBV_KOD", "OBEC_KOD", "POU_KOD", "ORP_KOD", "VUSC_KOD",
         "REGSOUDR_KOD", "STAT_KOD"),
    ),
}


class RuianSchemaError(RuntimeError):
    """A published file no longer matches the contracted header — stop, never guess."""


@dataclass(frozen=True, slots=True)
class Artifact:
    name: str
    url: str
    path: Path
    bytes: int
    sha256: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class AddressPointRow:
    """One row of the 19-column address CSV, all columns preserved."""

    kod_adm: int
    obec_kod: int | None
    obec_nazev: str | None
    momc_kod: int | None
    momc_nazev: str | None
    op_kod: int | None
    op_nazev: str | None
    cast_obce_kod: int | None
    cast_obce_nazev: str | None
    ulice_kod: int | None
    ulice_nazev: str | None
    typ_so: str | None
    cislo_domovni: int | None
    cislo_orientacni: int | None
    znak_orientacniho: str | None
    psc: str | None
    krovak: KrovakPositive | None
    plati_od: datetime.date | None


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def head(sess: requests.Session, url: str) -> dict[str, str] | None:
    """HEAD one artefact; None on 404. Returns the caching headers we persist."""
    resp = sess.head(url, timeout=_TIMEOUT, allow_redirects=True)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return {
        "url": url,
        "content_length": resp.headers.get("content-length", ""),
        "etag": resp.headers.get("etag", ""),
        "last_modified": resp.headers.get("last-modified", ""),
    }


def month_end(day: datetime.date) -> datetime.date:
    return day.replace(day=1) - datetime.timedelta(days=1)


def candidate_vintages(today: datetime.date, count: int = 3) -> list[datetime.date]:
    """The newest `count` month-end dates, newest first (04 C1.3: probe, don't compute)."""
    out: list[datetime.date] = []
    cursor = month_end(today)
    for _ in range(count):
        out.append(cursor)
        cursor = month_end(cursor)
    return out


def discover_vintage(
    sess: requests.Session,
    *,
    today: datetime.date | None = None,
    candidates: int = 3,
) -> datetime.date | None:
    """First month-end whose `strukt_ADR` product is published, newest first."""
    for vintage in candidate_vintages(today or datetime.date.today(), candidates):
        stamp = vintage.strftime("%Y%m%d")
        if head(sess, STRUKT_URL.format(vintage=stamp)) is not None:
            return vintage
    return None


def download(sess: requests.Session, name: str, url: str, dest: Path) -> Artifact:
    """Stream one artefact to disk, hashing as it goes."""
    digest = hashlib.sha256()
    size = 0
    with sess.get(url, timeout=_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(_CHUNK):
                if not chunk:
                    continue
                digest.update(chunk)
                size += len(chunk)
                fh.write(chunk)
        headers = resp.headers
    LOG.info("RUIAN download name=%s bytes=%d sha256=%s", name, size, digest.hexdigest()[:16])
    return Artifact(
        name=name,
        url=url,
        path=dest,
        bytes=size,
        sha256=digest.hexdigest(),
        etag=headers.get("etag") or None,
        last_modified=headers.get("last-modified") or None,
    )


def _reader(handle: io.BufferedIOBase) -> Iterator[list[str]]:
    return csv.reader(io.TextIOWrapper(handle, encoding="cp1250", newline=""), delimiter=";")


def _check_header(actual: list[str], expected: tuple[str, ...] | list[str], member: str) -> None:
    normalized = [c.strip().lstrip("﻿") for c in actual]
    if normalized != list(expected):
        raise RuianSchemaError(
            f"{member}: header drift\n  expected {list(expected)}\n  got      {normalized}"
        )


def _int(value: str | None) -> int | None:
    text = (value or "").strip()
    try:
        return int(text)
    except ValueError:
        return None


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _date(value: str | None) -> datetime.date | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_address_row(row: list[str]) -> AddressPointRow | None:
    """One CSV row -> the typed record. None when `Kód ADM` is unusable."""
    if len(row) < 19:
        return None
    kod_adm = _int(row[0])
    if kod_adm is None:
        return None
    return AddressPointRow(
        kod_adm=kod_adm,
        obec_kod=_int(row[1]),
        obec_nazev=_text(row[2]),
        momc_kod=_int(row[3]),
        momc_nazev=_text(row[4]),
        op_kod=_int(row[5]),
        op_nazev=_text(row[6]),
        cast_obce_kod=_int(row[7]),
        cast_obce_nazev=_text(row[8]),
        ulice_kod=_int(row[9]),
        ulice_nazev=_text(row[10]),
        typ_so=_text(row[11]),
        cislo_domovni=_int(row[12]),
        cislo_orientacni=_int(row[13]),
        znak_orientacniho=_text(row[14]),
        psc=_text(row[15]),
        krovak=KrovakPositive.from_csv(row[16], row[17]),
        plati_od=_date(row[18]),
    )


def iter_address_points(
    zip_path: Path,
    *,
    on_bad_coordinate: Callable[[int, str, str], None] | None = None,
) -> Iterator[AddressPointRow]:
    """Stream every address point of the OB_ADR product (national bundle or single obec).

    A coordinate that fails the positive-Křovák envelope does not abort the stream: it is
    reported to `on_bad_coordinate` and the row continues without coordinates, so the
    blocking control is the missing-coordinate assertion over the whole load rather than
    one poisoned row killing a 3 M-row parse.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not members:
            raise RuianSchemaError(f"{zip_path.name}: no CSV member found")
        for member in sorted(members):
            with zf.open(member) as handle:
                reader = _reader(handle)
                header = next(reader, None)
                if header is None:
                    continue
                _check_header(header, ADR_HEADER, member)
                for row in reader:
                    try:
                        parsed = parse_address_row(row)
                    except KrovakSignError:
                        if on_bad_coordinate is not None and len(row) >= 18:
                            on_bad_coordinate(_int(row[0]) or 0, row[16], row[17])
                        stripped = list(row)
                        stripped[16] = stripped[17] = ""
                        parsed = parse_address_row(stripped)
                    if parsed is not None:
                        yield parsed


def iter_strukt(zip_path: Path, key: str) -> Iterator[list[str]]:
    """Stream one member of the structured set, header-checked, blanks preserved."""
    member, header = STRUKT_MEMBERS[key]
    with zipfile.ZipFile(zip_path) as zf:
        names = {n.replace("\\", "/"): n for n in zf.namelist()}
        actual = names.get(member) or names.get(member.split("/")[-1])
        if actual is None:
            raise RuianSchemaError(f"{zip_path.name}: member {member} missing")
        with zf.open(actual) as handle:
            reader = _reader(handle)
            first = next(reader, None)
            if first is None:
                raise RuianSchemaError(f"{member}: empty file")
            _check_header(first, header, member)
            width = len(header)
            for row in reader:
                if not row:
                    continue
                yield [c.strip() for c in row[:width]] + [""] * max(0, width - len(row))
