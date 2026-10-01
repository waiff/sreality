"""Tests for the ScrapedListing -> write contract and the Gate-2 flag (scraper.db).

The listing write itself (birth, dirty and broker marks, the Gate-2 mint) is executed
against the replayed schema in tests/test_listing_write_live.py; the fake conn here only
serves the app_settings flag read.
"""

from __future__ import annotations

from typing import Any

from scraper import db, listing_write
from scraper.scraped_listing import ScrapedListing


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        for predicate, rows in self._conn.script:
            if predicate(s):
                self._rows = list(rows)
                self.rowcount = len(rows)
                return
        self._rows = []
        self.rowcount = 0

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]]) -> None:
        self.script = script
        self.executed: list[tuple[str, Any]] = []

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def _listing(**kw: Any) -> ScrapedListing:
    base = dict(source="bazos", source_id_native="218865547",
                source_url="https://bazos.cz/x", price_czk=20000, area_m2=50.0)
    base.update(kw)
    return ScrapedListing(**base)


# --- ScrapedListing contract ----------------------------------------------


def test_scraped_listing_content_hash_is_stable_and_price_sensitive():
    def h(**kw: Any) -> str:
        return listing_write.from_scraped(_listing(**kw)).content_hash

    a = h()
    assert a == h()
    assert a != h(price_czk=21000)
    assert a != h(description="nový popis")
    # source identity is NOT part of the content hash
    assert a == h(source_url="https://bazos.cz/other")
    # lat/lon are derived/geocoded and oscillation-prone (W4-c: a pin is a claim in
    # raw_json, never a column) — a coords-only change must NOT spawn a snapshot
    assert a == h(lat=50.0, lon=14.4)


def test_scraped_listing_columns_map_fields():
    row = _listing(disposition="2+kk", lat=50.0, lon=14.4).listing_columns()
    # The writer, not the contract, owns sreality_id (Gate 2 mints or leaves it NULL).
    assert "sreality_id" not in row
    # W4-c: the pin stays on the contract as parsed evidence, never as a column.
    assert "lat" not in row and "lon" not in row
    assert row["disposition"] == "2+kk"
    assert row["price_czk"] == 20000
    # sreality-only locality ids aren't carried; the writer defaults them to NULL.
    assert "locality_district_id" not in row
    assert set(row) == set(db.LISTING_COLUMNS)


# --- gate2_null_sreality_id_enabled flag (app_settings, read live) ---------


def test_gate2_flag_reads_default_false_when_setting_absent():
    conn = _FakeConn([])  # no app_settings row scripted -> fetchone() is None
    assert db._gate2_null_sreality_id_enabled(conn) is False


def test_gate2_flag_reads_true_from_jsonb_bool():
    conn = _FakeConn([
        (lambda s: "SELECT value FROM app_settings WHERE key" in s, [(True,)]),
    ])
    assert db._gate2_null_sreality_id_enabled(conn) is True


def test_gate2_flag_reads_false_from_jsonb_bool():
    conn = _FakeConn([
        (lambda s: "SELECT value FROM app_settings WHERE key" in s, [(False,)]),
    ])
    assert db._gate2_null_sreality_id_enabled(conn) is False


def test_gate2_flag_tolerates_string_true():
    conn = _FakeConn([
        (lambda s: "SELECT value FROM app_settings WHERE key" in s, [("true",)]),
    ])
    assert db._gate2_null_sreality_id_enabled(conn) is True
