"""Baseline load of the RÚIAN address register into the 01 §3 mirror tables (04 C1.7).

One `registry_version` per LOAD EVENT, composed of both products (00 §12.1): `strukt_ADR`
supplies the pre-joined admin chain, `OB_ADR` the 19 attribute columns incl. PSČ and the
positive-Křovák ordinates. Neither alone is sufficient, and the measured ~8 h 21 m
generation skew between them is handled by COUNTING — a `Kód ADM` present in one and
absent in the other becomes a `registry_load_discrepancies` row, never a silent preference.

Mechanism, in order (04 C1.7):
  1. HEAD-probe the vintage; download both zips, hashing as they stream.
  2. Archive both zips + a manifest to R2 (04 C1.8) BEFORE touching the database — an
     unarchived vintage stops being reproducible the moment ČÚZK rotates the directory.
  3. COPY into per-version UNLOGGED staging relations, no indexes, no constraints.
  4. Index + ANALYZE the staging relations.
  5. Run the blocking assertions AGAINST STAGING (golden point, counts, envelopes).
  6. Merge into the mirror: admin units, streets, address points + change log.
  7. Rebuild the gazetteer for this version.
  8. Publish = flip `is_current` in one short transaction.

A load is never partially visible: `is_current` moves only in the last step, and every step
is idempotent, so a killed run resumes from its checkpoint rather than restarting.

CLI:  python -m location_data.ruian_load [--vintage YYYYMMDD] [--dry-run] [--work-dir DIR]
Required: LOCATION_DB_DIRECT_URL or SUPABASE_DB_SESSION_URL, plus the R2_* archive vars.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import psycopg
import requests

from location_data import archive, krovak, load_assertions, loader_db, name_index, ruian_csv
from location_data.load_assertions import Assertion, PriorLoad, StagingStats

LOG = logging.getLogger("location_data.ruian_load")

# ltree labels are level-prefixed numeric codes, never names — the alphabet is
# [A-Za-z0-9_], so 'Brno-střed' is a syntax error at insert (01 §3.2.1). The design fixes
# k/o/b/c/p/u/m; the remaining five prefixes are chosen here and must stay stable.
LABEL_PREFIX: dict[str, str] = {
    "stat": "t",
    "region_soudrznosti": "g",
    "kraj": "k",
    "okres": "o",
    "orp": "p",
    "pou": "u",
    "obec": "b",
    "spravni_obvod": "s",
    "momc": "m",
    "cast_obce": "c",
    "katastralni_uzemi": "x",
    "zsj": "z",
}

# Hierarchy order — parents are upserted before children so parent_id/path always resolve.
LEVEL_ORDER: tuple[str, ...] = (
    "stat", "region_soudrznosti", "kraj", "okres", "orp", "pou", "obec",
    "spravni_obvod", "momc", "cast_obce", "katastralni_uzemi", "zsj",
)

MAX_DISCREPANCY_ROWS = 1000
MAX_RETIRE_FRACTION = 0.005

_STAGE_DDL = """
CREATE UNLOGGED TABLE IF NOT EXISTS {adr} (
  kod_adm bigint, obec_kod bigint, obec_nazev text, momc_kod bigint, momc_nazev text,
  op_kod bigint, op_nazev text, cobce_kod bigint, cobce_nazev text, ulice_kod bigint,
  ulice_nazev text, typ_so text, cislo_domovni integer, cislo_orientacni integer,
  znak text, psc text, krovak_y double precision, krovak_x double precision,
  plati_od date, lat double precision, lon double precision);
CREATE UNLOGGED TABLE IF NOT EXISTS {chain} (
  kod_adm bigint, ulice_kod bigint, cobce_kod bigint, momc_kod bigint, op_kod bigint,
  spravobv_kod bigint, obec_kod bigint, pou_kod bigint, orp_kod bigint, okres_kod bigint,
  vusc_kod bigint, vo_kod bigint);
CREATE UNLOGGED TABLE IF NOT EXISTS {cobce} (
  cobce_kod bigint, obec_kod bigint, pou_kod bigint, orp_kod bigint, okres_kod bigint,
  vusc_kod bigint, regsoudr_kod bigint, stat_kod bigint);
CREATE UNLOGGED TABLE IF NOT EXISTS {ulice} (ulice_kod bigint, obec_kod bigint);
CREATE UNLOGGED TABLE IF NOT EXISTS {katuz} (zsj_kod bigint, katuz_kod bigint, obec_kod bigint);
CREATE UNLOGGED TABLE IF NOT EXISTS {pou} (
  pou_kod bigint, orp_kod bigint, okres_kod bigint, vusc_kod bigint,
  regsoudr_kod bigint, stat_kod bigint);
CREATE UNLOGGED TABLE IF NOT EXISTS {momc} (
  momc_kod bigint, so_kod bigint, obec_kod bigint, pou_kod bigint, orp_kod bigint,
  vusc_kod bigint, regsoudr_kod bigint, stat_kod bigint);
CREATE UNLOGGED TABLE IF NOT EXISTS {units} (
  level text, code bigint, name text, name_norm text,
  parent_level text, parent_code bigint, is_placeholder boolean not null default false);
CREATE UNLOGGED TABLE IF NOT EXISTS {streets} (
  code bigint, name text, name_norm text, obec_kod bigint);
"""

_STAGE_INDEXES = """
CREATE INDEX IF NOT EXISTS {adr}_pk ON {adr} (kod_adm);
CREATE INDEX IF NOT EXISTS {chain}_pk ON {chain} (kod_adm);
CREATE INDEX IF NOT EXISTS {units}_pk ON {units} (level, code);
CREATE INDEX IF NOT EXISTS {streets}_pk ON {streets} (code);
"""

# The mirror columns compared to decide "did this address point change?".
_AP_COLUMNS = (
    "obec_unit_id", "obec_kod", "momc_unit_id", "praha_obvod_unit_id", "cast_obce_unit_id",
    "cast_obce_kod", "street_id", "ulice_kod", "typ_so", "cislo_domovni", "cislo_orientacni",
    "znak_orientacniho", "psc", "krovak_y_positive", "krovak_x_positive", "geom", "plati_od",
)


@dataclass(frozen=True, slots=True)
class Staging:
    adr: str
    chain: str
    cobce: str
    ulice: str
    katuz: str
    pou: str
    momc: str
    units: str
    streets: str
    diff: str

    @classmethod
    def for_version(cls, version_id: int) -> Staging:
        s = f"_v{version_id}"
        return cls(
            adr=f"ruian_stage_adr{s}", chain=f"ruian_stage_chain{s}",
            cobce=f"ruian_stage_cobce{s}", ulice=f"ruian_stage_ulice{s}",
            katuz=f"ruian_stage_katuz{s}", pou=f"ruian_stage_pou{s}",
            momc=f"ruian_stage_momc{s}", units=f"ruian_stage_units{s}",
            streets=f"ruian_stage_streets{s}", diff=f"ruian_stage_diff{s}",
        )

    def names(self) -> dict[str, str]:
        return {
            "adr": self.adr, "chain": self.chain, "cobce": self.cobce, "ulice": self.ulice,
            "katuz": self.katuz, "pou": self.pou, "momc": self.momc, "units": self.units,
            "streets": self.streets, "diff": self.diff,
        }


# ---------- version bookkeeping ----------


def version_label(vintage: datetime.date) -> str:
    return f"ruian:{vintage.isoformat()}"


def artifact_mismatches(
    stored_sha: dict[str, str] | None,
    stored_bytes: dict[str, int] | None,
    artifacts: dict[str, ruian_csv.Artifact],
) -> dict[str, dict[str, object]]:
    """Artefacts whose bytes changed under a label we already recorded.

    A vintage is supposed to be immutable. If ČÚZK republishes one under the same stamp,
    the recorded sha256 no longer describes what this run holds — and silently overwriting
    the record would make the version unreproducible and its assertions unauditable.
    """
    sha, size = dict(stored_sha or {}), dict(stored_bytes or {})
    out: dict[str, dict[str, object]] = {}
    for name, artifact in artifacts.items():
        previous_sha, previous_bytes = sha.get(name), size.get(name)
        if previous_sha and previous_sha != artifact.sha256:
            out[name] = {"stored_sha256": previous_sha, "fetched_sha256": artifact.sha256}
        elif previous_bytes is not None and int(previous_bytes) != artifact.bytes:
            out[name] = {"stored_bytes": int(previous_bytes), "fetched_bytes": artifact.bytes}
    return out


def ensure_version(
    conn: psycopg.Connection,
    vintage: datetime.date,
    artifacts: dict[str, ruian_csv.Artifact],
    proj: dict[str, str],
    *,
    archive_keys: dict[str, str] | None = None,
    allow_republished: bool = False,
) -> tuple[int, bool]:
    """Get or create the (not yet current) `registry_versions` row for this load event."""
    label = version_label(vintage)
    urls: dict[str, str] = {name: a.url for name, a in artifacts.items()}
    urls.update(archive_keys or {})
    payload = (
        json.dumps(urls),
        json.dumps({name: a.bytes for name, a in artifacts.items()}),
        json.dumps({name: a.sha256 for name, a in artifacts.items()}),
        json.dumps({name: a.etag for name, a in artifacts.items()}),
        json.dumps({name: a.last_modified for name, a in artifacts.items()}),
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, is_current, artifact_sha256, artifact_bytes FROM registry_versions "
            "WHERE label = %s",
            (label,),
        )
        row = cur.fetchone()
        if row is not None:
            version_id, is_current = int(row[0]), bool(row[1])
            mismatches = artifact_mismatches(row[2], row[3], artifacts)
            if mismatches and not allow_republished:
                loader_db.abort(
                    conn, version_id,
                    reason="artifact_republished",
                    detail={
                        "assertion": "artifact_bytes_are_immutable",
                        "label": label,
                        "artifacts": mismatches,
                        "hint": "ČÚZK republished this vintage; re-run with "
                                "--allow-republished to adopt the new bytes",
                    },
                )
            if mismatches:
                LOG.warning("RUIAN adopting republished artefacts for %s: %s", label, mismatches)
            cur.execute(
                """
                UPDATE registry_versions
                   SET artifact_urls = %s::jsonb, artifact_bytes = %s::jsonb,
                       artifact_sha256 = %s::jsonb, artifact_etag = %s::jsonb,
                       artifact_last_modified = %s::jsonb
                 WHERE id = %s
                """,
                (*payload, version_id),
            )
            return version_id, is_current
        cur.execute(
            """
            INSERT INTO registry_versions
                   (label, kind, source, source_date, artifact_urls, artifact_bytes,
                    artifact_sha256, artifact_etag, artifact_last_modified,
                    proj_version, proj_pipeline, is_current)
            VALUES (%s, 'baseline', 'vdp', %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, false)
            RETURNING id
            """,
            (label, vintage, *payload, proj["proj_version"], proj["proj_pipeline"]),
        )
        return int(cur.fetchone()[0]), False


def prior_load(
    conn: psycopg.Connection, *, exclude_version_id: int | None = None,
) -> PriorLoad | None:
    """Statistics of the last successfully published baseline — every growth-sensitive
    assertion is anchored to it, never to a 2026 constant (04 §4.5.3).

    `exclude_version_id` keeps a resumed run (published, then killed before the gazetteer)
    from anchoring its assertions to its own counts."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT row_counts, proj_pipeline
              FROM registry_versions
             WHERE is_current AND kind = 'baseline'
               AND (%(exclude)s::bigint IS NULL OR id <> %(exclude)s::bigint)
             LIMIT 1
            """,
            {"exclude": exclude_version_id},
        )
        row = cur.fetchone()
    if row is None:
        return None
    counts, pipeline = dict(row[0] or {}), row[1]
    if "address_points" not in counts:
        return None
    return PriorLoad(
        row_count=int(counts["address_points"]),
        missing_psc=int(counts.get("missing_psc", 0)),
        missing_coords=int(counts.get("missing_coords", 0)),
        krovak_y_min=float(counts.get("krovak_y_min", krovak.MEASURED_Y_ENVELOPE[0])),
        krovak_y_max=float(counts.get("krovak_y_max", krovak.MEASURED_Y_ENVELOPE[1])),
        krovak_x_min=float(counts.get("krovak_x_min", krovak.MEASURED_X_ENVELOPE[0])),
        krovak_x_max=float(counts.get("krovak_x_max", krovak.MEASURED_X_ENVELOPE[1])),
        discrepancies=int(counts.get("product_skew", 0)),
        proj_pipeline=pipeline,
    )


# ---------- staging ----------


def create_staging(conn: psycopg.Connection, stage: Staging) -> None:
    with conn.cursor() as cur:
        cur.execute(_STAGE_DDL.format(**stage.names()))


def truncate_staging(conn: psycopg.Connection, stage: Staging) -> None:
    with conn.cursor() as cur:
        for table in (stage.adr, stage.chain, stage.cobce, stage.ulice, stage.katuz,
                      stage.pou, stage.momc, stage.units, stage.streets):
            cur.execute(f"TRUNCATE {table}")


def drop_staging(conn: psycopg.Connection, stage: Staging) -> None:
    with conn.cursor() as cur:
        for table in stage.names().values():
            cur.execute(f"DROP TABLE IF EXISTS {table}")


def copy_address_points(
    conn: psycopg.Connection,
    stage: Staging,
    zip_path: Path,
    *,
    limit: int | None = None,
) -> dict[str, int]:
    """COPY the OB_ADR product into staging, converting Křovák -> WGS84 in Python via the
    ONE audited function (krovak.krovak_positive_to_wgs84). The 4326 column is then built
    with the immutable ST_SetSRID(ST_MakePoint(lon,lat),4326), never ST_Transform — whose
    pipeline choice is implicit and can move every coordinate by ~1 m on a PROJ upgrade."""
    bad: list[tuple[int, str, str]] = []
    rows = 0
    with conn.cursor() as cur:
        with cur.copy(
            f"COPY {stage.adr} (kod_adm, obec_kod, obec_nazev, momc_kod, momc_nazev, "
            "op_kod, op_nazev, cobce_kod, cobce_nazev, ulice_kod, ulice_nazev, typ_so, "
            "cislo_domovni, cislo_orientacni, znak, psc, krovak_y, krovak_x, plati_od, "
            "lat, lon) FROM STDIN"
        ) as copy:
            for row in ruian_csv.iter_address_points(
                zip_path, on_bad_coordinate=lambda k, y, x: bad.append((k, y, x))
            ):
                lat = lon = None
                if row.krovak is not None:
                    try:
                        point = krovak.krovak_positive_to_wgs84(row.krovak)
                        lat, lon = point.lat, point.lon
                    except krovak.KrovakSignError:
                        # Counted, not raised: one poisoned row must not kill a 3 M-row
                        # COPY, and the missing-coordinate assertion is the real control.
                        bad.append((row.kod_adm, str(row.krovak.y), str(row.krovak.x)))
                copy.write_row((
                    row.kod_adm, row.obec_kod, row.obec_nazev, row.momc_kod, row.momc_nazev,
                    row.op_kod, row.op_nazev, row.cast_obce_kod, row.cast_obce_nazev,
                    row.ulice_kod, row.ulice_nazev, row.typ_so, row.cislo_domovni,
                    row.cislo_orientacni, row.znak_orientacniho, row.psc,
                    row.krovak.y if row.krovak else None,
                    row.krovak.x if row.krovak else None,
                    row.plati_od, lat, lon,
                ))
                rows += 1
                if limit is not None and rows >= limit:
                    break
    if bad:
        LOG.warning("RUIAN %d rows carried out-of-envelope ordinates, e.g. %s", len(bad), bad[:3])
    return {"staged_address_points": rows, "bad_ordinates": len(bad)}


def _copy_strukt_member(
    conn: psycopg.Connection, table: str, columns: str, zip_path: Path, key: str,
) -> int:
    rows = 0
    with conn.cursor() as cur:
        with cur.copy(f"COPY {table} ({columns}) FROM STDIN") as copy:
            for row in ruian_csv.iter_strukt(zip_path, key):
                copy.write_row(tuple(int(v) if v else None for v in row))
                rows += 1
    return rows


def copy_strukt(conn: psycopg.Connection, stage: Staging, zip_path: Path) -> dict[str, int]:
    counts = {
        "staged_chain": _copy_strukt_member(
            conn, stage.chain,
            "kod_adm, ulice_kod, cobce_kod, momc_kod, op_kod, spravobv_kod, obec_kod, "
            "pou_kod, orp_kod, okres_kod, vusc_kod, vo_kod",
            zip_path, "chain"),
        "staged_cast_obce": _copy_strukt_member(
            conn, stage.cobce,
            "cobce_kod, obec_kod, pou_kod, orp_kod, okres_kod, vusc_kod, regsoudr_kod, stat_kod",
            zip_path, "cast_obce"),
        "staged_ulice": _copy_strukt_member(
            conn, stage.ulice, "ulice_kod, obec_kod", zip_path, "ulice"),
        "staged_katastr": _copy_strukt_member(
            conn, stage.katuz, "zsj_kod, katuz_kod, obec_kod", zip_path, "katastr"),
        "staged_pou": _copy_strukt_member(
            conn, stage.pou,
            "pou_kod, orp_kod, okres_kod, vusc_kod, regsoudr_kod, stat_kod", zip_path, "pou"),
    }
    momc = "momc_kod, so_kod, obec_kod, pou_kod, orp_kod, vusc_kod, regsoudr_kod, stat_kod"
    counts["staged_momc"] = (
        _copy_strukt_member(conn, stage.momc, momc, zip_path, "momc_statutarni")
        + _copy_strukt_member(conn, stage.momc, momc, zip_path, "momc_praha")
    )
    return counts


def index_staging(conn: psycopg.Connection, stage: Staging) -> None:
    with conn.cursor() as cur:
        cur.execute(_STAGE_INDEXES.format(**stage.names()))
        for table in (stage.adr, stage.chain, stage.cobce, stage.ulice, stage.katuz,
                      stage.pou, stage.momc):
            cur.execute(f"ANALYZE {table}")


# ---------- assertions ----------


def gather_stats(conn: psycopg.Connection, stage: Staging) -> StagingStats:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*), count(*) FILTER (WHERE psc IS NULL OR psc = ''),
                   count(*) FILTER (WHERE lat IS NULL OR lon IS NULL),
                   min(krovak_y), max(krovak_y), min(krovak_x), max(krovak_x),
                   min(lat), max(lat), min(lon), max(lon)
              FROM {stage.adr}
            """
        )
        (rows, missing_psc, missing_coords, y_min, y_max, x_min, x_max,
         lat_min, lat_max, lon_min, lon_max) = cur.fetchone()
        cur.execute(f"SELECT lat, lon FROM {stage.adr} WHERE kod_adm = %s",
                    (krovak.GOLDEN_KOD_ADM,))
        golden = cur.fetchone()
        cur.execute(
            f"""
            SELECT count(*) FILTER (WHERE c.kod_adm IS NULL),
                   (SELECT count(*) FROM {stage.chain} c2
                     WHERE NOT EXISTS (SELECT 1 FROM {stage.adr} a2 WHERE a2.kod_adm = c2.kod_adm))
              FROM {stage.adr} a LEFT JOIN {stage.chain} c ON c.kod_adm = a.kod_adm
            """
        )
        only_in_adr, only_in_chain = cur.fetchone()
    distance = None
    if golden is not None and golden[0] is not None:
        distance = krovak.golden_point_error_m(float(golden[0]), float(golden[1]))
    return StagingStats(
        row_count=int(rows), missing_psc=int(missing_psc), missing_coords=int(missing_coords),
        golden_distance_m=distance,
        krovak_y_min=y_min, krovak_y_max=y_max, krovak_x_min=x_min, krovak_x_max=x_max,
        lat_min=lat_min, lat_max=lat_max, lon_min=lon_min, lon_max=lon_max,
        only_in_adr=int(only_in_adr), only_in_chain=int(only_in_chain),
    )


def stats_to_counts(stats: StagingStats) -> dict[str, object]:
    return {
        "address_points": stats.row_count,
        "missing_psc": stats.missing_psc,
        "missing_coords": stats.missing_coords,
        "krovak_y_min": stats.krovak_y_min,
        "krovak_y_max": stats.krovak_y_max,
        "krovak_x_min": stats.krovak_x_min,
        "krovak_x_max": stats.krovak_x_max,
        "product_skew": stats.only_in_adr + stats.only_in_chain,
        "golden_point_error_m": stats.golden_distance_m,
    }


def report(assertions: list[Assertion]) -> None:
    for item in assertions:
        LOG.log(
            logging.INFO if item.ok else logging.ERROR,
            "ASSERT %s %s expected=%s actual=%s",
            item.name, "ok" if item.ok else f"FAILED[{item.route}]", item.expected, item.actual,
        )


def record_product_skew(conn: psycopg.Connection, version_id: int, stage: Staging) -> None:
    """A `Kód ADM` in one product and not the other is expected in small numbers because
    of the ~8 h 21 m generation skew — counted, never resolved by preferring a product."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO registry_load_discrepancies
                   (registry_version_id, entity_kind, entity_code, discrepancy, detail)
            SELECT %s, 'adresni_misto'::ruian_level, a.kod_adm,
                   'present_in_ob_adr_absent_in_strukt', '{{}}'::jsonb
              FROM {stage.adr} a
             WHERE NOT EXISTS (SELECT 1 FROM {stage.chain} c WHERE c.kod_adm = a.kod_adm)
             LIMIT %s
            ON CONFLICT DO NOTHING
            """,
            (version_id, MAX_DISCREPANCY_ROWS),
        )
        cur.execute(
            f"""
            INSERT INTO registry_load_discrepancies
                   (registry_version_id, entity_kind, entity_code, discrepancy, detail)
            SELECT %s, 'adresni_misto'::ruian_level, c.kod_adm,
                   'present_in_strukt_absent_in_ob_adr', '{{}}'::jsonb
              FROM {stage.chain} c
             WHERE NOT EXISTS (SELECT 1 FROM {stage.adr} a WHERE a.kod_adm = c.kod_adm)
             LIMIT %s
            ON CONFLICT DO NOTHING
            """,
            (version_id, MAX_DISCREPANCY_ROWS),
        )


# ---------- admin units ----------


def _fetch(conn: psycopg.Connection, sql: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def build_unit_rows(conn: psycopg.Connection, stage: Staging) -> int:
    """Derive one row per admin unit from staging and COPY it into the unit staging table.

    Names are normalized in Python (unaccent is STABLE — 01 §0.4) so `name_norm` has ONE
    definition shared with the gazetteer rebuild. Levels the CSV family carries no name
    for (stát, region soudržnosti, kraj, okres, ORP, POU, katastrální území, ZSJ) get the
    code as a placeholder name; the boundary pack (C4) fills them from its DBF, and the
    gazetteer skips any unit still named after its code.

    Praha carries NO okres in RÚIAN's own chain (134,585 address rows, verified against the
    20260731 vintage), so a unit whose okres is absent hangs off its kraj instead. Paths
    therefore skip absent levels rather than inventing one.
    """
    names: dict[str, dict[int, str]] = defaultdict(dict)
    for level, column, code_column in (
        ("obec", "obec_nazev", "obec_kod"),
        ("momc", "momc_nazev", "momc_kod"),
        ("spravni_obvod", "op_nazev", "op_kod"),
        ("cast_obce", "cobce_nazev", "cobce_kod"),
    ):
        for code, name in _fetch(
            conn,
            f"SELECT DISTINCT {code_column}, {column} FROM {stage.adr} "
            f"WHERE {code_column} IS NOT NULL AND {column} IS NOT NULL",
        ):
            names[level].setdefault(int(code), name)

    rows: dict[tuple[str, int], tuple[str, int | None]] = {}

    def add(level: str, code: int | None, parent_level: str | None, parent_code: int | None) -> None:
        if code is None:
            return
        key = (level, int(code))
        if key in rows and rows[key][1] is not None:
            return
        rows[key] = (parent_level or "", parent_code)

    chain = _fetch(
        conn,
        f"SELECT DISTINCT cobce_kod, obec_kod, pou_kod, orp_kod, okres_kod, vusc_kod, "
        f"regsoudr_kod, stat_kod FROM {stage.cobce}",
    )
    def add_backbone(stat: int | None, region: int | None, vusc: int | None,
                     okres: int | None, orp: int | None, pou: int | None) -> None:
        add("stat", stat, None, None)
        add("region_soudrznosti", region, "stat", stat)
        add("kraj", vusc, "region_soudrznosti", region)
        add("okres", okres, "kraj", vusc)
        if okres:
            add("orp", orp, "okres", okres)
        else:
            add("orp", orp, "kraj", vusc)
        add("pou", pou, "orp", orp)

    for cobce, obec, pou, orp, okres, vusc, region, stat in chain:
        add_backbone(stat, region, vusc, okres, orp, pou)
        if okres:
            add("obec", obec, "okres", okres)
        else:
            add("obec", obec, "kraj", vusc)
        add("cast_obce", cobce, "obec", obec)

    for pou, orp, okres, vusc, region, stat in _fetch(
        conn, f"SELECT DISTINCT pou_kod, orp_kod, okres_kod, vusc_kod, regsoudr_kod, "
              f"stat_kod FROM {stage.pou}"
    ):
        add_backbone(stat, region, vusc, okres, orp, pou)

    for momc, so, obec, *_ in _fetch(
        conn, f"SELECT DISTINCT momc_kod, so_kod, obec_kod FROM {stage.momc}"
    ):
        add("spravni_obvod", so, "obec", obec)
        add("momc", momc, "obec", obec)

    for code, obec in _fetch(
        conn, f"SELECT DISTINCT op_kod, obec_kod FROM {stage.adr} WHERE op_kod IS NOT NULL"
    ):
        add("spravni_obvod", code, "obec", obec)

    for zsj, katuz, obec in _fetch(
        conn, f"SELECT DISTINCT zsj_kod, katuz_kod, obec_kod FROM {stage.katuz}"
    ):
        add("katastralni_uzemi", katuz, "obec", obec)
        add("zsj", zsj, "katastralni_uzemi", katuz)

    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {stage.units}")
        with cur.copy(
            f"COPY {stage.units} (level, code, name, name_norm, parent_level, parent_code, "
            "is_placeholder) FROM STDIN"
        ) as copy:
            for (level, code), (parent_level, parent_code) in rows.items():
                real_name = names.get(level, {}).get(code)
                name = real_name or str(code)
                copy.write_row((
                    level, code, name, name_index.normalize_name(name),
                    parent_level or None, parent_code, real_name is None,
                ))
    return len(rows)


# The SCD-2 close predicate, stated once and mirrored by `unit_needs_new_version` (which is
# what the regression test exercises — CI has no Postgres).
#
# `is_placeholder` is the whole point: the CSV family carries no name for stát, region
# soudržnosti, kraj, okres, ORP, POU, katastrální území or ZSJ, so those rows stage as their
# own code and the boundary pack (C4) upgrades them to the real name afterwards. Comparing a
# staged PLACEHOLDER against the upgraded mirror name would close and re-open every one of
# those units on every monthly baseline, rewriting the tree and reverting the name.
_UNIT_CHANGED = (
    "((NOT s.is_placeholder AND u.name IS DISTINCT FROM s.name) "
    "OR u.parent_id IS DISTINCT FROM p.id)"
)

# The name a staged row must land with: a placeholder never overwrites a name the mirror
# already knows, so a parent-driven close+reopen carries the upgraded name forward.
_UNIT_NAME = "CASE WHEN s.is_placeholder THEN coalesce(prev.name, s.name) ELSE s.name END"
_UNIT_NAME_NORM = (
    "CASE WHEN s.is_placeholder THEN coalesce(prev.name_norm, s.name_norm) "
    "ELSE s.name_norm END"
)


def unit_needs_new_version(
    *,
    mirror_name: str,
    mirror_parent_id: int | None,
    staged_name: str,
    staged_is_placeholder: bool,
    staged_parent_id: int | None,
) -> bool:
    """Python mirror of `_UNIT_CHANGED` — same rule, testable without a database."""
    if mirror_parent_id != staged_parent_id:
        return True
    return not staged_is_placeholder and mirror_name != staged_name


def resolve_unit_name(
    *, staged_name: str, staged_is_placeholder: bool, previous_name: str | None,
) -> str:
    """Python mirror of `_UNIT_NAME`."""
    if staged_is_placeholder and previous_name:
        return previous_name
    return staged_name


def upsert_units(conn: psycopg.Connection, stage: Staging, version_id: int,
                 source_date: datetime.date) -> None:
    """SCD-2 per level: close a row whose real name or parent changed, open the replacement,
    and touch `last_version_id` on everything still present."""
    params = {"v": version_id, "d": source_date}
    for level in LEVEL_ORDER:
        prefix = LABEL_PREFIX[level]
        with conn.cursor() as cur, conn.transaction():
            cur.execute(
                f"""
                UPDATE ruian_admin_units u
                   SET valid_to = %(d)s, last_version_id = %(v)s
                  FROM {stage.units} s
             LEFT JOIN ruian_admin_units p
                    ON p.valid_to IS NULL AND p.level::text = s.parent_level
                   AND p.code = s.parent_code
                 WHERE s.level = %(lvl)s AND u.level::text = %(lvl)s AND u.code = s.code
                   AND u.valid_to IS NULL AND u.valid_from < %(d)s
                   AND {_UNIT_CHANGED}
                """,
                {**params, "lvl": level},
            )
            cur.execute(
                f"""
                INSERT INTO ruian_admin_units
                       (level, code, name, name_norm, parent_id, path, display_path,
                        valid_from, first_version_id, last_version_id)
                SELECT %(lvl)s::ruian_level, s.code, {_UNIT_NAME}, {_UNIT_NAME_NORM}, p.id,
                       (coalesce(p.path::text || '.', '')
                        || %(prefix)s::text || s.code::text)::ltree,
                       coalesce(p.display_path || ' / ', '') || {_UNIT_NAME},
                       %(d)s, %(v)s, %(v)s
                  FROM {stage.units} s
             LEFT JOIN ruian_admin_units p
                    ON p.valid_to IS NULL AND p.level::text = s.parent_level
                   AND p.code = s.parent_code
        LEFT JOIN LATERAL (
                     SELECT c.name, c.name_norm FROM ruian_admin_units c
                      WHERE c.level::text = %(lvl)s AND c.code = s.code
                        AND c.name <> c.code::text
                      ORDER BY c.valid_from DESC, c.id DESC
                      LIMIT 1) prev ON true
                 WHERE s.level = %(lvl)s
                   AND NOT EXISTS (
                         SELECT 1 FROM ruian_admin_units u
                          WHERE u.level::text = %(lvl)s AND u.code = s.code
                            AND u.valid_to IS NULL)
                ON CONFLICT (level, code, valid_from) DO NOTHING
                """,
                {**params, "lvl": level, "prefix": prefix},
            )
            cur.execute(
                f"""
                UPDATE ruian_admin_units u SET last_version_id = %(v)s
                  FROM {stage.units} s
                 WHERE s.level = %(lvl)s AND u.level::text = %(lvl)s AND u.code = s.code
                   AND u.valid_to IS NULL AND u.last_version_id <> %(v)s
                """,
                {**params, "lvl": level},
            )


def upsert_relations(conn: psycopg.Connection, stage: Staging, version_id: int) -> None:
    """The non-tree edges (01 §3.2): ORP/POU do not nest cleanly under okres and a
    katastrální území can straddle obec boundaries, so those edges are stored explicitly."""
    statements = (
        (f"""
         SELECT o.id, r.id, 'orp_of'
           FROM {stage.cobce} c
           JOIN ruian_admin_units o ON o.valid_to IS NULL AND o.level='obec' AND o.code=c.obec_kod
           JOIN ruian_admin_units r ON r.valid_to IS NULL AND r.level='orp' AND r.code=c.orp_kod
         """),
        (f"""
         SELECT o.id, u.id, 'pou_of'
           FROM {stage.cobce} c
           JOIN ruian_admin_units o ON o.valid_to IS NULL AND o.level='obec' AND o.code=c.obec_kod
           JOIN ruian_admin_units u ON u.valid_to IS NULL AND u.level='pou' AND u.code=c.pou_kod
         """),
        (f"""
         SELECT k.id, o.id, 'katastr_in_obec'
           FROM {stage.katuz} t
           JOIN ruian_admin_units k ON k.valid_to IS NULL AND k.level='katastralni_uzemi'
                                   AND k.code=t.katuz_kod
           JOIN ruian_admin_units o ON o.valid_to IS NULL AND o.level='obec' AND o.code=t.obec_kod
         """),
        (f"""
         SELECT m.id, s.id, 'momc_in_so'
           FROM {stage.momc} x
           JOIN ruian_admin_units m ON m.valid_to IS NULL AND m.level='momc' AND m.code=x.momc_kod
           JOIN ruian_admin_units s ON s.valid_to IS NULL AND s.level='spravni_obvod'
                                   AND s.code=x.so_kod
         """),
    )
    with conn.cursor() as cur:
        for select in statements:
            cur.execute(
                "INSERT INTO ruian_admin_unit_relations "
                "(from_id, to_id, relation_type, registry_version_id) "
                f"SELECT DISTINCT s.*, %s FROM ({select}) s "
                "ON CONFLICT DO NOTHING",
                (version_id,),
            )


def upsert_streets(conn: psycopg.Connection, stage: Staging, version_id: int,
                   source_date: datetime.date) -> int:
    """Streets, SCD-2 like units. Names normalized in Python with the street-word rule."""
    rows = _fetch(
        conn,
        f"""
        SELECT a.ulice_kod, min(a.ulice_nazev), min(coalesce(u.obec_kod, a.obec_kod))
          FROM {stage.adr} a
     LEFT JOIN {stage.ulice} u ON u.ulice_kod = a.ulice_kod
         WHERE a.ulice_kod IS NOT NULL AND a.ulice_nazev IS NOT NULL
      GROUP BY a.ulice_kod
        """,
    )
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {stage.streets}")
        with cur.copy(
            f"COPY {stage.streets} (code, name, name_norm, obec_kod) FROM STDIN"
        ) as copy:
            for code, name, obec_kod in rows:
                copy.write_row(
                    (code, name, name_index.normalize_street_name(name), obec_kod)
                )
    params = {"v": version_id, "d": source_date}
    with conn.cursor() as cur, conn.transaction():
        cur.execute(f"ANALYZE {stage.streets}")
        cur.execute(
            f"""
            UPDATE ruian_streets t SET valid_to = %(d)s, last_version_id = %(v)s
              FROM {stage.streets} s
         LEFT JOIN ruian_admin_units o
                ON o.valid_to IS NULL AND o.level = 'obec' AND o.code = s.obec_kod
             WHERE t.code = s.code AND t.valid_to IS NULL AND t.valid_from < %(d)s
               AND (t.name IS DISTINCT FROM s.name OR t.obec_unit_id IS DISTINCT FROM o.id)
            """,
            params,
        )
        cur.execute(
            f"""
            INSERT INTO ruian_streets
                   (code, name, name_norm, obec_unit_id, valid_from,
                    first_version_id, last_version_id)
            SELECT s.code, s.name, s.name_norm, o.id, %(d)s, %(v)s, %(v)s
              FROM {stage.streets} s
              JOIN ruian_admin_units o
                ON o.valid_to IS NULL AND o.level = 'obec' AND o.code = s.obec_kod
             WHERE NOT EXISTS (
                     SELECT 1 FROM ruian_streets t
                      WHERE t.code = s.code AND t.valid_to IS NULL)
            ON CONFLICT (code, valid_from) DO NOTHING
            """,
            params,
        )
        cur.execute(
            f"""
            UPDATE ruian_streets t SET last_version_id = %(v)s
              FROM {stage.streets} s
             WHERE t.code = s.code AND t.valid_to IS NULL AND t.last_version_id <> %(v)s
            """,
            params,
        )
    return len(rows)


# ---------- address points ----------


def _resolved_source(stage: Staging) -> str:
    return f"""
        SELECT a.kod_adm,
               ob.id AS obec_unit_id, a.obec_kod,
               mo.id AS momc_unit_id, po.id AS praha_obvod_unit_id,
               co.id AS cast_obce_unit_id, a.cobce_kod AS cast_obce_kod,
               st.id AS street_id, a.ulice_kod,
               a.typ_so, a.cislo_domovni, a.cislo_orientacni,
               a.znak AS znak_orientacniho, a.psc::char(5) AS psc,
               a.krovak_y AS krovak_y_positive, a.krovak_x AS krovak_x_positive,
               CASE WHEN a.lat IS NOT NULL AND a.lon IS NOT NULL
                    THEN ST_SetSRID(ST_MakePoint(a.lon, a.lat), 4326) END AS geom,
               a.plati_od
          FROM {stage.adr} a
          JOIN ruian_admin_units ob
            ON ob.valid_to IS NULL AND ob.level = 'obec' AND ob.code = a.obec_kod
     LEFT JOIN ruian_admin_units mo
            ON mo.valid_to IS NULL AND mo.level = 'momc' AND mo.code = a.momc_kod
     LEFT JOIN ruian_admin_units po
            ON po.valid_to IS NULL AND po.level = 'spravni_obvod' AND po.code = a.op_kod
     LEFT JOIN ruian_admin_units co
            ON co.valid_to IS NULL AND co.level = 'cast_obce' AND co.code = a.cobce_kod
     LEFT JOIN ruian_streets st
            ON st.valid_to IS NULL AND st.code = a.ulice_kod
         WHERE a.psc IS NOT NULL AND a.plati_od IS NOT NULL
    """


def load_address_points(conn: psycopg.Connection, stage: Staging, version_id: int,
                        source_date: datetime.date) -> dict[str, int]:
    """Upsert the mirror and append one change-log row per CHANGED address point.

    `before_row` is captured into a diff relation BEFORE the upsert because Postgres 17's
    `ON CONFLICT ... RETURNING` cannot expose the pre-update row.
    """
    src = _resolved_source(stage)
    compare = ", ".join(f"t.{c}" for c in _AP_COLUMNS)
    compare_src = ", ".join(f"s.{c}" for c in _AP_COLUMNS)
    columns = ", ".join(_AP_COLUMNS)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {stage.diff}")
        cur.execute(
            f"""
            CREATE UNLOGGED TABLE {stage.diff} AS
            SELECT s.kod_adm,
                   CASE WHEN t.kod_adm IS NULL THEN 'insert'
                        WHEN t.valid_to IS NOT NULL THEN 'reinstate'
                        ELSE 'update' END AS change_kind,
                   -- to_jsonb() of an unmatched LEFT JOIN row is an all-null OBJECT, not
                   -- SQL NULL, so an insert would carry a fake before_row without this.
                   CASE WHEN t.kod_adm IS NULL THEN NULL ELSE to_jsonb(t) END AS before_row
              FROM ({src}) s
         LEFT JOIN ruian_address_points t ON t.kod_adm = s.kod_adm
             WHERE t.kod_adm IS NULL OR t.valid_to IS NOT NULL
                OR ({compare}) IS DISTINCT FROM ({compare_src})
            """
        )
        cur.execute(f"CREATE INDEX ON {stage.diff} (kod_adm)")
        cur.execute(f"ANALYZE {stage.diff}")
        changed = loader_db.scalar(conn, f"SELECT count(*) FROM {stage.diff}")

        cur.execute(
            f"""
            INSERT INTO ruian_address_points
                   (kod_adm, {columns}, valid_to, first_version_id, last_version_id)
            SELECT s.kod_adm, {compare_src}, NULL, %(v)s, %(v)s
              FROM ({src}) s
              JOIN {stage.diff} d ON d.kod_adm = s.kod_adm
            ON CONFLICT (kod_adm) DO UPDATE SET
                   {", ".join(f"{c} = EXCLUDED.{c}" for c in _AP_COLUMNS)},
                   valid_to = NULL,
                   last_version_id = EXCLUDED.last_version_id
            """,
            {"v": version_id},
        )
        cur.execute(
            f"""
            INSERT INTO ruian_address_point_changes
                   (kod_adm, registry_version_id, change_kind, changed_fields,
                    before_row, after_row)
            SELECT d.kod_adm, %(v)s, d.change_kind,
                   CASE WHEN d.before_row IS NULL THEN '{{}}'::text[]
                        ELSE ARRAY(SELECT key FROM jsonb_each(to_jsonb(t))
                                    WHERE to_jsonb(t) -> key IS DISTINCT FROM d.before_row -> key)
                   END,
                   d.before_row, to_jsonb(t)
              FROM {stage.diff} d
              JOIN ruian_address_points t ON t.kod_adm = d.kod_adm
            ON CONFLICT (kod_adm, registry_version_id) DO NOTHING
            """,
            {"v": version_id},
        )

    live = loader_db.scalar(
        conn, "SELECT count(*) FROM ruian_address_points WHERE valid_to IS NULL"
    )
    gone = loader_db.scalar(
        conn,
        f"""
        SELECT count(*) FROM ruian_address_points t
         WHERE t.valid_to IS NULL
           AND NOT EXISTS (SELECT 1 FROM {stage.adr} a WHERE a.kod_adm = t.kod_adm)
        """,
    )
    if live and gone and gone > live * MAX_RETIRE_FRACTION:
        loader_db.abort(
            conn, version_id,
            reason="retire_fraction",
            detail={
                "assertion": "absent_from_baseline",
                "expected": f"<= {MAX_RETIRE_FRACTION:.3%} of {live}",
                "actual": gone,
                "staging": stage.adr,
            },
        )
    with conn.cursor() as cur, conn.transaction():
        cur.execute(
            f"""
            INSERT INTO ruian_address_point_changes
                   (kod_adm, registry_version_id, change_kind, changed_fields,
                    before_row, after_row)
            SELECT t.kod_adm, %(v)s, 'retire', '{{}}'::text[], to_jsonb(t), NULL
              FROM ruian_address_points t
             WHERE t.valid_to IS NULL
               AND NOT EXISTS (SELECT 1 FROM {stage.adr} a WHERE a.kod_adm = t.kod_adm)
            ON CONFLICT (kod_adm, registry_version_id) DO NOTHING
            """,
            {"v": version_id},
        )
        cur.execute(
            f"""
            UPDATE ruian_address_points t
               SET valid_to = %(d)s, last_version_id = %(v)s
             WHERE t.valid_to IS NULL
               AND NOT EXISTS (SELECT 1 FROM {stage.adr} a WHERE a.kod_adm = t.kod_adm)
            """,
            {"v": version_id, "d": source_date},
        )
    with conn.cursor() as cur:
        cur.execute("ANALYZE ruian_address_points")
    return {"changed_address_points": int(changed or 0), "retired_address_points": int(gone or 0)}


def unloadable_rows(conn: psycopg.Connection, stage: Staging) -> int:
    """Staged address points the mirror cannot accept: no obec unit, or a NULL in one of
    the mirror's NOT NULL columns. Counted and reported, never silently dropped."""
    return int(loader_db.scalar(
        conn,
        f"""
        SELECT count(*) FROM {stage.adr} a
         WHERE a.psc IS NULL OR a.plati_od IS NULL
            OR NOT EXISTS (
                 SELECT 1 FROM ruian_admin_units o
                  WHERE o.valid_to IS NULL AND o.level = 'obec' AND o.code = a.obec_kod)
        """,
    ) or 0)


def publish(conn: psycopg.Connection, version_id: int) -> None:
    """The only step that changes what the platform reads. Short, transactional, last."""
    with conn.cursor() as cur, conn.transaction():
        cur.execute(
            "UPDATE registry_versions SET is_current = false WHERE is_current AND id <> %s",
            (version_id,),
        )
        cur.execute(
            "UPDATE registry_versions SET is_current = true WHERE id = %s", (version_id,)
        )


# ---------- dry run ----------


def dry_run_stats(zip_path: Path, *, limit: int | None = None) -> StagingStats:
    """Everything the blocking assertions need, computed without a database."""
    rows = missing_psc = missing_coords = 0
    y_min = x_min = lat_min = lon_min = None
    y_max = x_max = lat_max = lon_max = None
    golden: float | None = None
    for row in ruian_csv.iter_address_points(zip_path):
        rows += 1
        if not row.psc:
            missing_psc += 1
        point = None
        if row.krovak is not None:
            try:
                point = krovak.krovak_positive_to_wgs84(row.krovak)
            except krovak.KrovakSignError:
                point = None
        if point is None:
            missing_coords += 1
        else:
            y_min = row.krovak.y if y_min is None else min(y_min, row.krovak.y)
            y_max = row.krovak.y if y_max is None else max(y_max, row.krovak.y)
            x_min = row.krovak.x if x_min is None else min(x_min, row.krovak.x)
            x_max = row.krovak.x if x_max is None else max(x_max, row.krovak.x)
            lat_min = point.lat if lat_min is None else min(lat_min, point.lat)
            lat_max = point.lat if lat_max is None else max(lat_max, point.lat)
            lon_min = point.lon if lon_min is None else min(lon_min, point.lon)
            lon_max = point.lon if lon_max is None else max(lon_max, point.lon)
            if row.kod_adm == krovak.GOLDEN_KOD_ADM:
                golden = krovak.golden_point_error_m(point.lat, point.lon)
        if limit is not None and rows >= limit:
            break
    return StagingStats(
        row_count=rows, missing_psc=missing_psc, missing_coords=missing_coords,
        golden_distance_m=golden, krovak_y_min=y_min, krovak_y_max=y_max,
        krovak_x_min=x_min, krovak_x_max=x_max, lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
    )


# ---------- orchestration ----------


def fetch_artifacts(
    sess: requests.Session, vintage: datetime.date, work_dir: Path, *, reuse: bool,
) -> dict[str, ruian_csv.Artifact]:
    stamp = vintage.strftime("%Y%m%d")
    wanted = {
        "csv_strukt_adr": ruian_csv.STRUKT_URL.format(vintage=stamp),
        "csv_ob_adr": ruian_csv.OB_ADR_URL.format(vintage=stamp),
    }
    out: dict[str, ruian_csv.Artifact] = {}
    for name, url in wanted.items():
        dest = work_dir / url.rsplit("/", 1)[-1]
        if reuse and dest.exists():
            digest = hashlib.sha256()
            with dest.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
            out[name] = ruian_csv.Artifact(
                name=name, url=url, path=dest, bytes=dest.stat().st_size,
                sha256=digest.hexdigest(), etag=None, last_modified=None,
            )
            LOG.info("RUIAN reusing cached %s", dest)
            continue
        out[name] = ruian_csv.download(sess, name, url, dest)
    return out


def run(
    *,
    vintage: datetime.date | None,
    work_dir: Path,
    dry_run: bool,
    reuse: bool,
    keep_staging: bool,
    limit: int | None,
    allow_unarchived: bool = False,
    allow_republished: bool = False,
) -> int:
    sess = ruian_csv.session()
    if vintage is None:
        vintage = ruian_csv.discover_vintage(sess)
        if vintage is None:
            LOG.error("RUIAN no published vintage found in the last 3 month-ends")
            return 1
    LOG.info("RUIAN vintage=%s", vintage.isoformat())
    artifacts = fetch_artifacts(sess, vintage, work_dir, reuse=reuse)
    proj = krovak.proj_environment()
    LOG.info("RUIAN proj=%s pipeline=%s", proj["proj_version"], proj["proj_pipeline"])

    if dry_run:
        stats = dry_run_stats(artifacts["csv_ob_adr"].path, limit=limit)
        assertions = load_assertions.evaluate(stats, None, proj_pipeline=proj["proj_pipeline"])
        report(assertions)
        return 1 if load_assertions.blocking_failures(assertions) else 0

    # BEFORE any staging (04 §C1.8): a vintage that is not archived is a registry_version
    # that stops being reproducible the moment ČÚZK rotates the CSV directory, so a failed
    # upload aborts the load rather than publishing an unreproducible version.
    archive_keys: dict[str, str] = {}
    try:
        archive_keys = archive.archive_version(version_label(vintage), artifacts)
    except archive.ArchiveError as exc:
        if not allow_unarchived:
            LOG.error("RUIAN %s", exc)
            return 1
        LOG.warning("RUIAN loading UNARCHIVED (--allow-unarchived): %s", exc)

    with loader_db.open_loader_connection() as conn:
        version_id, already_current = ensure_version(
            conn, vintage, artifacts, proj,
            archive_keys=archive_keys, allow_republished=allow_republished,
        )
        stage = Staging.for_version(version_id)
        progress = loader_db.read_progress(conn, version_id)
        # Resume on the phase checkpoint, NOT on is_current: a run killed between the
        # pointer swap and the gazetteer rebuild leaves a current version with zero
        # ruian_name_index rows, and short-circuiting on is_current would make that
        # unrecoverable without hand-editing the row.
        if already_current and loader_db.phase_done(progress, "published"):
            LOG.info("RUIAN version %s is already current and complete — nothing to do",
                     version_id)
            return 0
        prior = prior_load(conn, exclude_version_id=version_id)

        create_staging(conn, stage)
        if not loader_db.phase_done(progress, "staged"):
            truncate_staging(conn, stage)
            counts = copy_address_points(conn, stage, artifacts["csv_ob_adr"].path, limit=limit)
            counts.update(copy_strukt(conn, stage, artifacts["csv_strukt_adr"].path))
            index_staging(conn, stage)
            loader_db.write_progress(conn, version_id, phase="staged", counts=counts)
            LOG.info("RUIAN staged %s", counts)

        stats = gather_stats(conn, stage)
        assertions = load_assertions.evaluate(stats, prior, proj_pipeline=proj["proj_pipeline"])
        report(assertions)
        loader_db.write_progress(conn, version_id, phase="asserted",
                                 counts=stats_to_counts(stats))
        failures = load_assertions.blocking_failures(assertions)
        if failures:
            loader_db.abort(
                conn, version_id,
                reason="assertion_failed",
                detail={
                    "assertion": failures[0].name,
                    "expected": failures[0].expected,
                    "actual": failures[0].actual,
                    "staging": stage.adr,
                    "all_failed": [f.name for f in failures],
                },
            )
        record_product_skew(conn, version_id, stage)

        if not loader_db.phase_done(progress, "units"):
            units = build_unit_rows(conn, stage)
            with conn.cursor() as cur:
                cur.execute(f"ANALYZE {stage.units}")
            upsert_units(conn, stage, version_id, vintage)
            upsert_relations(conn, stage, version_id)
            loader_db.write_progress(conn, version_id, phase="units",
                                     counts={"admin_units": units})
            LOG.info("RUIAN admin units staged=%d", units)

        if not loader_db.phase_done(progress, "streets"):
            streets = upsert_streets(conn, stage, version_id, vintage)
            loader_db.write_progress(conn, version_id, phase="streets",
                                     counts={"streets": streets})
            LOG.info("RUIAN streets=%d", streets)

        orphans = unloadable_rows(conn, stage)
        if orphans:
            loader_db.record_discrepancy(
                conn, version_id, entity_kind="adresni_misto", entity_code=0,
                discrepancy="address_point_not_loadable", detail={"rows": orphans},
            )
            LOG.warning("RUIAN %d staged address points are not loadable", orphans)

        if not loader_db.phase_done(progress, "points"):
            counts = load_address_points(conn, stage, version_id, vintage)
            loader_db.write_progress(conn, version_id, phase="points", counts=counts)
            LOG.info("RUIAN address points %s", counts)

        # The gazetteer is rebuilt BEFORE the pointer swap: publishing first and dying
        # mid-rebuild would leave the version every resolution binds to with zero
        # ruian_name_index rows. The rebuild is delete-then-insert per version, so a
        # resumed run repeats it harmlessly.
        if not loader_db.phase_done(progress, "gazetteer"):
            rebuilt = name_index.rebuild(conn, version_id)
            loader_db.write_progress(conn, version_id, phase="gazetteer",
                                     counts={"name_index": rebuilt})
            LOG.info("RUIAN gazetteer rows=%d", rebuilt)

        publish(conn, version_id)
        loader_db.write_progress(conn, version_id, phase="published")
        LOG.info("RUIAN published version=%s label=%s", version_id, version_label(vintage))

        if not keep_staging:
            drop_staging(conn, stage)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vintage", help="YYYYMMDD (default: HEAD-probe the newest published)")
    parser.add_argument("--work-dir", default=None, help="where the zips are downloaded")
    parser.add_argument("--reuse-downloads", action="store_true",
                        help="reuse already-downloaded zips in --work-dir")
    parser.add_argument("--dry-run", action="store_true",
                        help="download + parse + assert, no database access")
    parser.add_argument("--keep-staging", action="store_true",
                        help="keep the per-version staging relations after publish")
    parser.add_argument("--limit", type=int, default=None,
                        help="stage only the first N address points (testing)")
    parser.add_argument("--allow-unarchived", action="store_true",
                        help="load even if the R2 vintage archive (04 C1.8) failed — the "
                             "version becomes unreproducible once ČÚZK rotates; state why")
    parser.add_argument("--allow-republished", action="store_true",
                        help="adopt artefact bytes that differ from the ones already "
                             "recorded under this vintage label")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not args.dry_run and not (
        os.environ.get("SUPABASE_DB_SESSION_URL") or os.environ.get("LOCATION_DB_DIRECT_URL")
    ):
        print(
            "ERROR: set LOCATION_DB_DIRECT_URL (a direct 5432 URL) or SUPABASE_DB_SESSION_URL "
            "— a registry load needs a session it owns, not the transaction pooler.",
            file=sys.stderr,
        )
        return 2

    vintage = (
        datetime.datetime.strptime(args.vintage, "%Y%m%d").date() if args.vintage else None
    )
    with tempfile.TemporaryDirectory(prefix="ruian-") as tmp:
        work_dir = Path(args.work_dir) if args.work_dir else Path(tmp)
        work_dir.mkdir(parents=True, exist_ok=True)
        try:
            return run(
                vintage=vintage, work_dir=work_dir, dry_run=args.dry_run,
                reuse=args.reuse_downloads, keep_staging=args.keep_staging, limit=args.limit,
                allow_unarchived=args.allow_unarchived,
                allow_republished=args.allow_republished,
            )
        except loader_db.LoadAborted as exc:
            LOG.error("RUIAN load aborted: %s", exc)
            return 1


if __name__ == "__main__":
    sys.exit(main())
