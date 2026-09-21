"""Hermetic tests for scraper.sold_db (no DB).

A fake connection records every statement. The SQL itself is checked against the
real schema by the CI PREPARE sweep (tests/test_sql_schema_prepare.py); what is
pinned here is what that sweep cannot see — the payload the statements are handed,
and the four clauses the work-list means.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import fields
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from scraper import sold_db
from scraper.reas_parser import SoldTransaction


def _row(**over: Any) -> SoldTransaction:
    values: dict[str, Any] = {
        "source": "reas",
        "source_record_id": "12345_building_678",
        "sold_at": dt.date(2026, 8, 21),
        "price_czk": 5_400_000,
        "asking_last_czk": 5_700_000,
        "listed_at": dt.datetime(2026, 5, 1, 9, 0),
        "published_at": dt.datetime(2026, 9, 20, 9, 0),
        "category_main": "dum",
        "category_type": "prodej",
        "subtype": "rodinny_dum",
        "disposition": None,
        "area_m2": 120.0,
        "area_basis": "usable",
        "usable_area": 120.0,
        "estate_area": 640.0,
        "geom": "SRID=4326;POINT(17.25 49.6)",
        "address_text": "Dlouhá 1, Olomouc",
        "obec_kod": 500496,
        "ku_kod": None,
        "ulice_kod": None,
        "photo_urls": ["https://img/1.jpg"],
        "source_url": "https://www.reas.cz/prodane/inzerat-x",
        "raw": {"type": "building"},
    }
    values.update(over)
    return SoldTransaction(**values)


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((" ".join(sql.split()), params))

    def fetchone(self) -> tuple | None:
        return self._conn.rows.pop(0) if self._conn.rows else None

    def fetchall(self) -> list[tuple]:
        rows, self._conn.rows = self._conn.rows, []
        return rows


class _FakeConn:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.rows = list(rows or [])

    def cursor(self) -> _Cur:
        return _Cur(self)


# --- the cell box ------------------------------------------------------------


def test_the_box_is_the_obec_envelope_widened_by_the_shared_read_radius():
    conn = _FakeConn([(49.45, 17.05, 49.75, 17.45, "SRID=4326;POLYGON((...))")])

    box = sold_db.obec_cell_box(conn, 500496)

    sql, params = conn.executed[0]
    assert params == {"margin_m": 5000.0, "obec_kod": 500496}
    # `admin_boundaries.id` IS the RÚIAN kód (migration 017); `sreality_id` is the
    # other id space and joining it here would silently box the wrong town.
    assert "ab.level = 'obec' AND ab.id = %(obec_kod)s" in sql
    assert "sreality_id" not in sql
    # geography in, so the margin is metres — an uncast ST_Buffer is degrees.
    assert "::geography," in sql
    assert box == sold_db.CellBox(49.45, 17.05, 49.75, 17.45, "SRID=4326;POLYGON((...))")


def test_an_obec_without_a_polygon_has_no_box():
    assert sold_db.obec_cell_box(_FakeConn(), 999999) is None


def test_the_box_margin_is_the_read_surface_s_largest_radius():
    # W3's block offers 1/3/5 km; expanding every cell by the widest one means any
    # subject inside the obec is covered to its full radius by construction.
    assert sold_db.MAX_READ_RADIUS_M == 5000


# --- the work-list -----------------------------------------------------------


def test_an_unmigrated_database_answers_none_not_an_empty_list():
    # "Nothing to do" and "this database cannot answer" must not read the same.
    assert sold_db.sold_comp_cells(_FakeConn([(False,)]), "reas", cap=5) is None


def test_the_work_list_passes_its_cell_ttls_and_cap():
    conn = _FakeConn([(True,), (554782,), (500496,)])

    assert sold_db.sold_comp_cells(conn, "reas", cap=5) == [554782, 500496]
    assert conn.executed[1][1] == {
        "source": "reas", "ok_ttl": 35 * 24 * 3600.0, "failed_ttl": 6 * 3600.0,
        "cap": 5,
    }


def test_the_work_list_means_a_live_card_on_an_active_flat_or_house():
    sql = " ".join(sold_db._SOLD_COMP_CELLS_SQL.split())
    # DISTINCT over accounts: two accounts carding one property is ONE cell, and one
    # of them removing their card must not stop the other's fetch.
    assert "SELECT DISTINCT ll.obec_kod" in sql
    assert "ps.archived_at IS NULL" in sql and "NOT ps.is_terminal" in sql
    assert "p.status = 'active'" in sql and "p.is_active" in sql
    # reas's sold catalogue is byty + domy only, so no other category earns a fetch.
    assert "p.category_main IN ('byt', 'dum')" in sql
    # The subject's point comes from the property's repr listing, never from listings.
    assert "ll.listing_id = p.repr_listing_ref_id" in sql


def test_a_cell_that_cannot_be_boxed_is_never_offered_as_work():
    # An obec absent from the boundary ingest has no box, so a fetch can only skip
    # it — writing no ledger row, leaving `fetched_at` NULL, and sorting FIRST again
    # on every pass for ever. Excluding it in SQL is what bounds that.
    sql = " ".join(sold_db._SOLD_COMP_CELLS_SQL.split())
    assert "JOIN admin_boundaries ab ON ab.level = 'obec' AND ab.id = ll.obec_kod" in sql


def test_freshness_is_subtracted_from_the_newest_ledger_row_per_cell():
    sql = " ".join(sold_db._SOLD_COMP_CELLS_SQL.split())
    # The ledger is append-only, so a cell's state is its LAST row and nothing else.
    assert "ORDER BY f.fetched_at DESC, f.id DESC LIMIT 1" in sql
    assert "newest.fetched_at IS NULL" in sql, "a never-looked-at cell must qualify"
    assert "ORDER BY newest.fetched_at NULLS FIRST" in sql


# --- the upsert --------------------------------------------------------------


def test_nothing_is_written_for_an_empty_cell():
    conn = _FakeConn()
    assert sold_db.upsert_sold_transactions(conn, []) == (0, 0)
    assert conn.executed == []


def test_every_parser_field_is_a_column_of_the_write():
    # Both directions: a field the parser adds and the write forgets would be lost
    # silently, and a column the write names that no field fills would not PREPARE.
    declared = re.search(r"AS j\((.*?)\)\s*ON CONFLICT",
                         sold_db._UPSERT_SOLD_SQL, re.DOTALL)
    assert declared is not None
    columns = {part.split()[0] for part in declared.group(1).split(",")}
    assert columns == {f.name for f in fields(SoldTransaction)}


def test_a_page_that_carries_one_sale_twice_still_writes():
    # ON CONFLICT cannot touch one row twice in a single statement, so a duplicated
    # record would abort the whole cell rather than lose one row.
    conn = _FakeConn([(True,)])

    stored, new = sold_db.upsert_sold_transactions(
        conn, [_row(price_czk=1), _row(price_czk=2)])

    payload = conn.executed[0][1]["rows"]
    assert isinstance(payload, Jsonb)
    assert [r["price_czk"] for r in payload.obj] == [2], "last sighting wins"
    # Two records parsed, ONE sale stored. The ledger counts this number, so a walk
    # whose page boundary shifted cannot over-report against the table it wrote.
    assert (stored, new) == (1, 1)


def test_a_re_seen_sale_overwrites_and_restamps_when_we_last_looked():
    sql = " ".join(sold_db._UPSERT_SOLD_SQL.split())
    assert "ON CONFLICT (source, source_record_id) DO UPDATE" in sql
    assert "fetched_at = EXCLUDED.fetched_at" in sql
    # The parser hands EWKT; the geometry is built in SQL, never in Python.
    assert "ST_GeomFromEWKT(j.geom)" in sql


def test_the_payload_is_json_serializable_with_its_dates_and_arrays():
    # A sold row carries a date, two timestamps and a text[]; what makes those
    # jsonb-able is the process-wide serializer `scraper.db` installs, which
    # `sold_db` imports for exactly this reason.
    from scraper.db import _jsonb_dumps

    conn = _FakeConn([(True,)])
    sold_db.upsert_sold_transactions(conn, [_row()])
    encoded = _jsonb_dumps(conn.executed[0][1]["rows"].obj)
    assert '"sold_at": "2026-08-21"' in encoded
    assert '"photo_urls": ["https://img/1.jpg"]' in encoded


# --- the ledger --------------------------------------------------------------


@pytest.mark.parametrize("status", ["ok", "failed"])
def test_one_ledger_row_per_cell_attempt(status: str):
    conn = _FakeConn()

    sold_db.record_fetch(
        conn, source="reas", obec_kod=500496, bbox="SRID=4326;POLYGON((...))",
        status=status, record_count=89, source_total=625, pages=1,
    )

    sql, params = conn.executed[0]
    assert sql.startswith("INSERT INTO sold_transaction_fetches")
    assert params["status"] == status
    assert params["record_count"] == 89
    # `source_total` is the source's count WITHOUT our date window — the only number
    # that can say how much of the cell the table is not seeing (Olomouc 89 of 625).
    assert params["source_total"] == 625
