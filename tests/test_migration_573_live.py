"""Migration 573, EXECUTED over seeded hits and misses (PR #1630 review).

The replay runs 573 on an empty schema, which proves only that it compiles. Here one row
per rail that the file must clear and one beside it that it must keep are seeded, the whole
file is run, and each row is read back: the NULL (area_basis with area_m2), the backup
row carrying the old value and the rail, the property queued for maintenance — and
nothing for a miss. A second run changes nothing. Runs in CI's migrations job
(`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from scraper.area import derive_headline_area
from scraper.attribute_contract import source_value
from scraper.floor import floor_from_portal, total_floors_from_portal

_DB_URL = os.environ.get("TEST_DATABASE_URL")

_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

_SQL = (Path(__file__).resolve().parent.parent / "migrations"
        / "573_portal_contract_leftovers_floor_area.sql").read_text(encoding="utf-8")
_SREALITY_IDS = itertools.count(9_573_000_001)

# (label, source, column, value, category_main, disposition, area_basis, raw podlaží, rail)
# rail None = a miss the file must keep.
_ROWS: tuple[tuple[Any, ...], ...] = (
    ("f1 placeholder", "idnes", "floor", 20, "byt", "2+kk", None, "20. patro a vyšší", "F1"),
    ("f1 miss 19", "idnes", "floor", 19, "byt", "2+kk", None, "19. patro", None),
    ("f1 miss sreality 20", "sreality", "floor", 20, "byt", "2+kk", None, None, None),
    ("f2 above", "sreality", "floor", 161, "byt", "1+1", None, None, "F2"),
    ("f2 below", "realitymix", "floor", -4, "byt", "1+1", None, None, "F2"),
    ("f2 miss top", "sreality", "floor", 40, "byt", "1+1", None, None, None),
    ("f2 miss bottom", "ceskereality", "floor", -3, "byt", "1+1", None, None, None),
    ("t1 above", "bezrealitky", "total_floors", 113, "byt", "3+1", None, None, "T1"),
    ("t1 zero", "mmreality", "total_floors", 0, "byt", "3+1", None, None, "T1"),
    ("t1 miss top", "bezrealitky", "total_floors", 40, "byt", "3+1", None, None, None),
    ("t1 miss one", "mmreality", "total_floors", 1, "dum", "3+1", None, None, None),
    ("a0 cellar", "bazos", "area_m2", 2.0, "byt", None, "unknown", None, "A0"),
    ("a0 hall", "idnes", "area_m2", 4.9, "komercni", None, "usable", None, "A0"),
    ("a0 miss hall", "idnes", "area_m2", 5.0, "komercni", None, "usable", None, None),
    ("a0 miss parcel", "bazos", "area_m2", 2.0, "pozemek", None, "plot", None, None),
    ("a0 miss ostatni", "bazos", "area_m2", 3.0, "ostatni", None, "unknown", None, None),
    ("a1 room", "bazos", "area_m2", 23.9, "byt", "3+1", "unknown", None, "A1"),
    ("a1 house", "bazos", "area_m2", 7.9, "dum", "1+kk", "unknown", None, "A1"),
    ("a1 miss on band", "bazos", "area_m2", 24.0, "byt", "3+1", "unknown", None, None),
    ("a1 miss atypical", "bazos", "area_m2", 6.0, "byt", "atypicky", "unknown", None, None),
    ("a1 miss hall", "bazos", "area_m2", 6.0, "komercni", "3+1", "unknown", None, None),
    ("a2 site", "sreality", "area_m2", 1000.0, "byt", "2+kk", "usable", None, "A2"),
    ("a2 miss flat", "sreality", "area_m2", 999.9, "byt", "2+kk", "usable", None, None),
    ("a2 miss house", "idnes", "area_m2", 1800.0, "dum", "5+1", "usable", None, None),
    ("a2 miss hall", "ceskereality", "area_m2", 1800.0, "komercni", None, "usable", None, None),
    # The house ceiling on an UNLABELLED figure has no backfill here (a named residual).
    ("residual dum parcel", "bazos", "area_m2", 1256.0, "dum", None, "unknown", None, None),
)


@pytest.fixture()
def cur():
    import psycopg

    conn = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=60000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=60000",
    )
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


def _seed(cur: Any, row: tuple[Any, ...]) -> tuple[int, int]:
    _, source, column, value, category, disposition, basis, podlazi, _ = row
    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    pid = int(cur.fetchone()[0])
    raw = {"params": {"podlaží": podlazi}} if podlazi else {}
    cells = {"floor": None, "total_floors": None, "area_m2": 70.0, "area_basis": "usable"}
    cells[column] = value
    if column == "area_m2":
        cells["area_basis"] = basis
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, category_main, "
        "category_type, price_czk, disposition, floor, total_floors, area_m2, area_basis, "
        "is_active, property_id) VALUES (%s, %s, %s, %s::jsonb, %s, 'prodej', 5000000, %s, %s, "
        "%s, %s, %s, true, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"m573-{uuid.uuid4()}", json.dumps(raw), category, disposition, cells["floor"],
         cells["total_floors"], cells["area_m2"], cells["area_basis"], pid),
    )
    return int(cur.fetchone()[0]), pid


def _python_declines(row: tuple[Any, ...]) -> bool:
    """The parser rail's verdict on the same stored value — the equivalence 573 claims."""
    _, source, column, value, category, disposition, _, podlazi, _ = row
    if column == "floor":
        if source == "idnes":
            return value == 20 and source_value("idnes", "floor", {"podlaží": podlazi}) is None
        return floor_from_portal("ground0", str(value)) is None
    if column == "total_floors":
        return total_floors_from_portal(value) is None
    return derive_headline_area(category_main=category, usable=value,
                                disposition=disposition) == (None, None)


def _read(cur: Any, listing_id: int, column: str) -> tuple[Any, Any]:
    cur.execute(f"SELECT {column}, area_basis FROM listings WHERE id = %s", (listing_id,))
    return cur.fetchone()


def _backup(cur: Any, listing_id: int, column: str) -> tuple[Any, ...] | None:
    cur.execute("SELECT rail, old_value, old_basis FROM backup_a4.listing_cells "
                "WHERE listing_id = %s AND column_name = %s", (listing_id, column))
    return cur.fetchone()


def _queued(cur: Any, pid: int) -> bool:
    cur.execute("SELECT 1 FROM dirty_properties WHERE property_id = %s", (pid,))
    return cur.fetchone() is not None


def test_every_seeded_row_matches_the_parser_rail():
    for row in _ROWS:
        if row[0] == "residual dum parcel":
            continue
        assert _python_declines(row) == (row[-1] is not None), row[0]


@_needs_db
def test_the_file_clears_exactly_the_hits_backs_them_up_and_queues_them(cur):
    seeded = [(row, *_seed(cur, row)) for row in _ROWS]
    cur.execute("DELETE FROM dirty_properties WHERE property_id = ANY(%s)",
                ([pid for _, _, pid in seeded],))

    cur.execute(_SQL)

    for row, listing_id, pid in seeded:
        label, _, column, value, _, _, basis, _, rail = row
        stored, stored_basis = _read(cur, listing_id, column)
        backup = _backup(cur, listing_id, column)
        if rail is None:
            assert float(stored) == float(value), label
            assert backup is None, label
            assert not _queued(cur, pid), label
            continue
        assert stored is None, label
        if column == "area_m2":
            assert stored_basis is None, label
        assert backup is not None, label
        assert backup[0] == rail, label
        assert float(backup[1]) == float(value), label
        assert backup[2] == (basis if column == "area_m2" else None), label
        assert _queued(cur, pid), label


@_needs_db
def test_a_second_run_changes_nothing(cur):
    seeded = [(row, *_seed(cur, row)) for row in _ROWS]
    cur.execute(_SQL)
    cur.execute("SELECT count(*) FROM backup_a4.listing_cells WHERE listing_id = ANY(%s)",
                ([listing_id for _, listing_id, _ in seeded],))
    first = cur.fetchone()[0]
    assert first == sum(1 for row in _ROWS if row[-1] is not None)

    cur.execute(_SQL)
    cur.execute("SELECT count(*) FROM backup_a4.listing_cells WHERE listing_id = ANY(%s)",
                ([listing_id for _, listing_id, _ in seeded],))
    assert cur.fetchone()[0] == first
    for row, listing_id, _ in seeded:
        if row[-1] is None:
            assert float(_read(cur, listing_id, row[2])[0]) == float(row[3]), row[0]
