"""Parse the MF "Cenová mapa nájemného" XLSX and compute reference rents.

The Ministry of Finance publishes a quarterly hedonic-model rent map. Sheet 2
("Cenové mapy nájemného") carries one row per territory with four horizontal
VK (size-category) blocks; sheet 1 ("Základní informace") carries the per-VK
amenity adjustment tables (one for the older reference flat, one for new
builds). We ingest the per-block column *Nájemné referenčního bytu za m²* (and
its novostavba twin) plus the two adjustment tables; every interval / min /
max / median column is deliberately ignored (pre-hedonic-model raw data).

Parsing is stdlib-only (`zipfile` + `xml.etree`) — an XLSX is a zip of XML and
the layout is flat tabular, so `openpyxl` would be a needless dependency.

The territory key `Kód obce` is the ČÚZK/RÚIAN code; it equals
`admin_boundaries.id`. A non-empty *Katastrální území* cell means the code is a
cadastral-area (`ku`) code; an empty one means it is an `obec` code — verified
exact against the live `admin_boundaries` table (zero ku/obec id collision).
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    import psycopg

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# Amenity adjustment keys applied to the reference rent, in display order.
ADJUSTMENT_ATTRIBUTES: tuple[str, ...] = (
    "balcony", "terrace", "furnished", "garage", "elevator", "other_material",
)
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Normalised header fragment -> stable internal attribute name. The sheet ships
# a "vybavennost" double-n typo, so we match on a diacritic-stripped prefix.
_ADJ_FRAGMENTS: tuple[tuple[str, str], ...] = (
    ("jiny typ konstrukcni", "other_material"),
    ("balk", "balcony"),
    ("teras", "terrace"),
    ("vybaven", "furnished"),
    ("garaz", "garage"),
    ("vytah", "elevator"),
)


@dataclass(frozen=True)
class RentValue:
    ruian_code: int
    level: str  # 'ku' | 'obec'
    kraj: str | None
    ku_name: str | None
    obec_name: str | None
    vk: int
    ref_rent_per_m2: int | None
    ref_rent_novostavba_per_m2: int | None
    data_coverage: int | None


@dataclass(frozen=True)
class RentAdjustment:
    vk: int
    is_novostavba: bool
    attribute: str
    czk_per_m2: int


@dataclass(frozen=True)
class ParsedRentMap:
    values: list[RentValue]
    adjustments: list[RentAdjustment]
    source_date: date | None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_date_from_filename(name: str) -> date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def disposition_to_vk(disposition: str | None) -> int | None:
    """Map a sreality disposition to a VK size category (1..4)."""
    if not disposition:
        return None
    m = re.match(r"\s*(\d+)", disposition)
    if not m:
        return None
    n = int(m.group(1))
    if n <= 1:
        return 1
    if n >= 4:
        return 4
    return n


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _to_int(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _col_to_idx(ref: str) -> tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)", ref)
    assert m is not None
    col = 0
    for ch in m.group(1):
        col = col * 26 + (ord(ch) - 64)
    return col, int(m.group(2))


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(t.text or "" for t in si.iter(f"{_NS}t"))
        for si in root.findall(f"{_NS}si")
    ]


def _read_sheet(
    z: zipfile.ZipFile, path: str, ss: list[str]
) -> dict[tuple[int, int], str]:
    """Return {(row, col_idx): text} for one worksheet (1-based row/col)."""
    root = ET.fromstring(z.read(path))
    cells: dict[tuple[int, int], str] = {}
    for c in root.iter(f"{_NS}c"):
        ref = c.get("r")
        if not ref:
            continue
        t = c.get("t")
        v = c.find(f"{_NS}v")
        isn = c.find(f"{_NS}is")
        val: str | None = None
        if t == "s" and v is not None and v.text is not None:
            val = ss[int(v.text)]
        elif isn is not None:
            val = "".join(x.text or "" for x in isn.iter(f"{_NS}t"))
        elif v is not None:
            val = v.text
        if val is None or val == "":
            continue
        col, row = _col_to_idx(ref)
        cells[(row, col)] = val
    return cells


def _resolve_sheets(z: zipfile.ZipFile) -> dict[str, str]:
    """Return {sheet_name: worksheet_xml_path}."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rid_target = {rel.get("Id"): rel.get("Target") for rel in rels}
    out: dict[str, str] = {}
    for sh in wb.iter(f"{_NS}sheet"):
        name = sh.get("name") or ""
        rid = sh.get(f"{_RNS}id")
        target = rid_target.get(rid) or ""
        if target and not target.startswith("/"):
            target = "xl/" + target
        out[name] = target.lstrip("/")
    return out


def _find_sheet(sheets: dict[str, str], *needles: str) -> str | None:
    for name, path in sheets.items():
        low = _norm(name)
        if all(_norm(n) in low for n in needles):
            return path
    return None


def _row_headers(cells: dict[tuple[int, int], str], row: int) -> dict[int, str]:
    return {c: v for (r, c), v in cells.items() if r == row}


def _classify_attr(header: str) -> str | None:
    n = _norm(header)
    for frag, attr in _ADJ_FRAGMENTS:
        if frag in n:
            return attr
    return None


def _parse_values(cells: dict[tuple[int, int], str]) -> list[RentValue]:
    headers = _row_headers(cells, 1)
    by_text: dict[str, list[int]] = {}
    col_kraj = col_ku = col_obec = col_code = None
    for col, text in headers.items():
        n = _norm(text)
        by_text.setdefault(n, []).append(col)
        if n == "kraj":
            col_kraj = col
        elif "katastralni uzemi" in n:
            col_ku = col
        elif n == "obec":
            col_obec = col
        elif "kod obce" in n:
            col_code = col

    def cols_for(predicate) -> list[int]:
        return sorted(
            c for c, text in headers.items() if predicate(_norm(text))
        )

    vk_cols = cols_for(lambda n: n == "vk")
    ref_cols = cols_for(
        lambda n: n.startswith("najemne referencniho bytu za m")
    )
    nov_cols = cols_for(lambda n: "referencniho bytu novostavby" in n)
    cov_cols = cols_for(lambda n: "datova pokrytost" in n)

    if not (col_code and vk_cols and ref_cols):
        raise ValueError("rent map: could not locate header columns in sheet 2")
    nblocks = len(vk_cols)
    if not (len(ref_cols) == len(nov_cols) == len(cov_cols) == nblocks):
        raise ValueError("rent map: mismatched VK block columns in sheet 2")

    max_row = max(r for r, _ in cells)
    out: list[RentValue] = []
    for row in range(2, max_row + 1):
        raw_code = cells.get((row, col_code))
        code = _to_int(raw_code)
        if code is None:
            continue
        kraj = cells.get((row, col_kraj)) if col_kraj else None
        ku_name = cells.get((row, col_ku)) if col_ku else None
        obec_name = cells.get((row, col_obec)) if col_obec else None
        level = "ku" if ku_name else "obec"
        for i in range(nblocks):
            vk = _to_int(cells.get((row, vk_cols[i]))) or (i + 1)
            out.append(
                RentValue(
                    ruian_code=code,
                    level=level,
                    kraj=kraj,
                    ku_name=ku_name,
                    obec_name=obec_name,
                    vk=vk,
                    ref_rent_per_m2=_to_int(cells.get((row, ref_cols[i]))),
                    ref_rent_novostavba_per_m2=_to_int(
                        cells.get((row, nov_cols[i]))
                    ),
                    data_coverage=_to_int(cells.get((row, cov_cols[i]))),
                )
            )
    return out


def _parse_adjustments(
    cells: dict[tuple[int, int], str]
) -> list[RentAdjustment]:
    titles: list[tuple[int, bool]] = []
    for (row, col), text in cells.items():
        n = _norm(text)
        if "prehled cen cenotvornych parametru" in n:
            titles.append((row, "novostavb" in n))
    out: list[RentAdjustment] = []
    for title_row, is_nov in sorted(titles):
        header_row = title_row + 1
        attr_cols = {
            col: attr
            for col, text in _row_headers(cells, header_row).items()
            if (attr := _classify_attr(text))
        }
        if not attr_cols:
            continue
        for dr in range(header_row + 1, header_row + 12):
            label = None
            for col in range(1, min(attr_cols)):
                v = cells.get((dr, col))
                if v and _norm(v).startswith("vk"):
                    label = v
                    break
            if not label:
                break
            m = re.search(r"(\d+)", label)
            if not m:
                break
            vk = int(m.group(1))
            for col, attr in attr_cols.items():
                czk = _to_int(cells.get((dr, col)))
                if czk is not None:
                    out.append(RentAdjustment(vk, is_nov, attr, czk))
    return out


def _sheet_source_date(cells: dict[tuple[int, int], str]) -> date | None:
    months = {
        "lednu": 1, "unoru": 2, "breznu": 3, "dubnu": 4, "kvetnu": 5,
        "cervnu": 6, "cervenci": 7, "srpnu": 8, "zari": 9, "rijnu": 10,
        "listopadu": 11, "prosinci": 12,
    }
    for text in cells.values():
        n = _norm(text)
        if "popisne vystupy" in n:
            m = re.search(r"k ([a-z]+) (\d{4})", n)
            if m and m.group(1) in months:
                return date(int(m.group(2)), months[m.group(1)], 1)
    return None


def parse_rent_map_xlsx(
    data: bytes, *, source_date: date | None = None
) -> ParsedRentMap:
    """Parse the MF rent-map workbook into typed value + adjustment rows."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        ss = _shared_strings(z)
        sheets = _resolve_sheets(z)
        values_path = _find_sheet(sheets, "cenove mapy najemneho") or _find_sheet(
            sheets, "najemneho"
        )
        info_path = _find_sheet(sheets, "zakladni informace")
        if not values_path or not info_path:
            raise ValueError("rent map: expected sheets not found in workbook")
        value_cells = _read_sheet(z, values_path, ss)
        info_cells = _read_sheet(z, info_path, ss)

    values = _parse_values(value_cells)
    adjustments = _parse_adjustments(info_cells)
    if not values:
        raise ValueError("rent map: no territory rows parsed")
    if not adjustments:
        raise ValueError("rent map: no adjustment rows parsed")
    return ParsedRentMap(
        values=values,
        adjustments=adjustments,
        source_date=source_date or _sheet_source_date(info_cells),
    )


_REFERENCE_RENT_SQL = """
    select v.ruian_code, v.level, v.kraj,
           v.ref_rent_per_m2, v.ref_rent_novostavba_per_m2,
           v.source_revision, r.source_date,
           coalesce(b.name, v.ku_name, v.obec_name) as name
      from rent_map_values_public v
      join admin_boundaries_public b
        on b.id = v.ruian_code and b.level = v.level
      join rent_map_revisions r on r.source_revision = v.source_revision
     where v.vk = %(vk)s
       and st_covers(
             b.geom,
             st_setsrid(st_makepoint(%(lng)s, %(lat)s), 4326)::geography)
     order by case b.level when 'ku' then 0 else 1 end
     limit 1
"""


def compute_reference_rent(
    conn: "psycopg.Connection",
    *,
    lat: float | None,
    lng: float | None,
    area_m2: float | None,
    disposition: str | None,
    amenities: dict[str, Any] | None,
    is_novostavba: bool = False,
) -> dict[str, Any] | None:
    """MF Cenová mapa secondary rent reference for one subject.

    Resolves the subject's territory by point-in-polygon against
    `admin_boundaries`, picks the VK from the disposition, reads the base
    reference rent + the published per-amenity adjustments from the latest
    ingested revision, and scales by area. Read-only. Best-effort: returns
    None on any miss (no revision, territory not covered, missing area /
    disposition / base rent) and never raises.
    """
    try:
        if lat is None or lng is None or not area_m2 or area_m2 <= 0:
            return None
        vk = disposition_to_vk(disposition)
        if vk is None:
            return None
        amenities = amenities or {}
        with conn.cursor() as cur:
            cur.execute(
                _REFERENCE_RENT_SQL,
                {"vk": vk, "lat": float(lat), "lng": float(lng)},
            )
            row = cur.fetchone()
            if row is None:
                return None
            (ruian_code, level, kraj, ref_std, ref_nov, source_revision,
             source_date, name) = row
            base = ref_nov if is_novostavba else ref_std
            if base is None:
                return None
            cur.execute(
                "select attribute, czk_per_m2 from rent_map_adjustments_public "
                "where vk = %(vk)s and is_novostavba = %(nov)s",
                {"vk": vk, "nov": is_novostavba},
            )
            adj_table = {a: c for a, c in cur.fetchall()}
    except Exception:
        return None

    applied = [
        {"attribute": attr, "czk_per_m2": int(adj_table[attr])}
        for attr in ADJUSTMENT_ATTRIBUTES
        if amenities.get(attr) and attr in adj_table
    ]
    total_per_m2 = int(base) + sum(a["czk_per_m2"] for a in applied)
    return {
        "territory": {
            "ruian_code": int(ruian_code),
            "level": level,
            "name": name,
            "kraj": kraj,
        },
        "vk": vk,
        "is_novostavba": bool(is_novostavba),
        "source_revision": int(source_revision),
        "source_date": source_date.isoformat() if source_date else None,
        "base_per_m2": int(base),
        "adjustments": applied,
        "total_per_m2": total_per_m2,
        "area_m2": round(float(area_m2), 2),
        "monthly_rent_czk": int(round(total_per_m2 * float(area_m2))),
    }

