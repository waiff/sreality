"""The spaced-thousands heal: the same page, re-read correctly, and nothing else written.

This job re-parses a listing's own ARCHIVED detail body with the fixed grammar and writes
back the area columns. What has to hold: the selection covers every row a truncation could
have touched and no wide column (a `raw_json` predicate detoasts every candidate — the cost
that killed `backfill_mmreality_areas`'s first dispatch); the write touches areas only,
never price and never a snapshot; the numeric ceiling is applied by the SAME guard the
ingest path uses, so an unstorable parcel becomes NULL instead of a 22003 that aborts the
page; a dry run writes nothing; and a healed row re-parses to itself, so a second pass is a
no-op.

The fake connection below is a small in-memory `listings`, so the write is actually applied
and the idempotence claim is exercised rather than asserted.
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper.area import MAX_AREA_M2
from scripts import backfill_area_spaced_thousands as mod

NB = " "

_CK_URL = "https://www.ceskereality.cz/prodej/pozemky/praha/prodej-pozemku-1234567.html"
_RM_URL = "https://www.remax-czech.cz/reality/detail/445483/prodej-pozemku"

# ceskereality: a parcel its own page renders with a thousands group. The naive grammar
# read 870 and stored it as the headline — which is why NOT ONE of its 19,088 land rows
# had an area_m2 of 1000 or more.
CK_BODY = (
    f"<html><body><h1>Prodej pozemku 5{NB}870 m²</h1>"
    '<dl class="g-info"><div class="i-info">'
    '<span class="i-info__title">Plocha pozemku</span>'
    f'<span class="i-info__value">5{NB}870 m²</span></div></dl>'
    "</body></html>"
).encode("utf-8")

# remax: a parcel `area_m2` (numeric(7,1)) cannot store. The headline must come out NULL —
# declined by the one rule — while `estate_area` (numeric(9,1)) keeps it.
RM_BODY = (
    '<html><head><title>t</title></head><body>'
    f'<h1 class="pd-header__title">Prodej pozemku 16{NB}809{NB}800 m², Brno</h1>'
    '<div class="pd-detail-info"><div class="pd-detail-info__row">'
    '<div class="pd-detail-info__label">Plocha pozemku:</div>'
    f'<div class="pd-detail-info__value">16{NB}809{NB}800 m²</div></div></div>'
    "</body></html>"
).encode("utf-8")


# --- a tiny in-memory listings + payload archive ------------------------------


class _Row(dict):
    """One `listings` row, addressed the way the script's SELECT projects it."""


def _listing(**kw: Any) -> _Row:
    row = _Row(id=0, source="ceskereality", source_id_native="x", source_url=_CK_URL,
               category_main="pozemek", category_type="prodej", property_id=None,
               area_m2=None, usable_area=None, estate_area=None, garden_area=None,
               area_basis=None, is_active=True)
    row.update(kw)
    return row


_PROJECTION = ("id", "source", "source_id_native", "source_url", "category_main",
               "category_type", "property_id", "area_m2", "usable_area",
               "estate_area", "garden_area")


class _Store:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []

    def download_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return self.objects[key]


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple] = []
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        params = params or {}
        self._conn.executed.append(sql)
        if "GROUP BY source" in sql:
            self._rows = [(s, 1, 1, 1) for s in sorted(
                {r["source"] for r in self._conn.listings})]
        elif "portal_raw_payloads p" in sql:
            self._rows = [(s, n, pid) for (s, n), pid
                          in sorted(self._conn.payloads.items())
                          if n in params["natives"] and s in params["sources"]]
        elif "FROM portal_raw_payloads" in sql:
            # `page_readers.load_bodies` own statement: id, body, body_r2_key, encoding.
            self._rows = [(pid, None, key, "identity")
                          for pid, key in sorted(self._conn.bodies.items())
                          if pid in params["ids"]]
        elif sql.strip().startswith("SELECT id"):
            rows = sorted((r for r in self._conn.listings
                           if r["id"] > params["after"]
                           and r["source"] in params["sources"]),
                          key=lambda r: r["id"])
            self._rows = [tuple(r[c] for c in _PROJECTION)
                          for r in rows[:params["page"]]]
        elif sql.startswith("SET LOCAL "):
            self._conn.guards.append(sql)
        elif sql.strip().startswith("UPDATE listings"):
            self._conn.updates.append(params)
            by_id = {r["id"]: r for r in self._conn.listings}
            for i, lid in enumerate(params["ids"]):
                row = by_id[lid]
                for col in (*mod._AREA_COLS, "area_basis"):
                    row[col] = params[col][i]
            self.rowcount = len(params["ids"])
        else:  # pragma: no cover - the script issues no other statement
            raise AssertionError(f"unexpected SQL: {sql[:80]}")

    def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _Tx:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Tx":
        self._conn.transactions += 1
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Conn:
    def __init__(self, listings: list[_Row], payloads: dict[tuple[str, str], int],
                 bodies: dict[int, str]) -> None:
        self.listings = listings
        self.payloads = payloads
        self.bodies = bodies
        self.executed: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.guards: list[str] = []
        self.transactions = 0

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx(self)

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _run(monkeypatch: pytest.MonkeyPatch, conn: _Conn, store: _Store,
         *argv: str) -> None:
    from location_data import payloads as payloads_mod

    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://fake")
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(mod.db, "mark_properties_dirty",
                        lambda _c, ids: len(list(ids)))
    monkeypatch.setattr(payloads_mod, "open_store", lambda: store)
    monkeypatch.setattr("sys.argv",
                        ["backfill_area_spaced_thousands", *argv])
    assert mod.main() == 0


# --- the selection ------------------------------------------------------------


def test_the_suspect_predicate_is_spelled_once_and_used_everywhere() -> None:
    assert mod._SUSPECT in mod._COUNT_BY_SOURCE_SQL
    assert mod._SUSPECT in mod._SELECT_SQL
    # a truncation leaves the last three digits ...
    for col in mod._AREA_COLS:
        assert f"{col} < 1000" in mod._SUSPECT
    # ... unless they were "000", which the write boundary NULLed: the all-NULL arm.
    assert "area_m2 IS NULL AND usable_area IS NULL" in " ".join(mod._SUSPECT.split())


def test_the_selection_touches_no_wide_column() -> None:
    """A `raw_json->>'title'` arm would detoast every candidate row — the cost that killed
    `backfill_mmreality_areas`'s first live dispatch on a 120 s statement_timeout — and it
    covers nothing the all-NULL arm does not."""
    for statement in (mod._SUSPECT, mod._SELECT_SQL, mod._COUNT_BY_SOURCE_SQL):
        assert "raw_json" not in statement and "description" not in statement
    assert "property_id" in mod._SELECT_SQL.split("FROM")[0]  # rule 20


def test_the_payload_read_takes_the_latest_successful_detail_body() -> None:
    sql = " ".join(mod._PAYLOADS_SQL.split())
    assert "DISTINCT ON (p.source, p.source_id_native)" in sql
    assert "p.page_kind = 'detail'" in sql
    assert "p.version_seq DESC NULLS LAST" in sql
    # NULL ranks as successful, exactly as location_data.payloads ranks it.
    assert "p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299" in sql


def test_the_write_touches_areas_only() -> None:
    sql = " ".join(mod._UPDATE_SQL.split())
    assert sql.count("UPDATE ") == 1
    assert "price" not in sql and "listing_snapshots" not in sql
    for col in (*mod._AREA_COLS, "area_basis"):
        assert f"{col} = u.{col}" in sql
    assert any("statement_timeout" in g for g in mod._BATCH_GUARDS)
    assert all(g.startswith("SET LOCAL ") for g in mod._BATCH_GUARDS)


def test_bazos_is_not_in_the_healed_population() -> None:
    """It shares the fixed grammar from this PR on, but its area comes from free ad text:
    that corpus was never measured, and a re-parse would move numbers nobody has counted."""
    assert "bazos" not in mod.DEFAULT_SOURCES
    assert set(mod.DEFAULT_SOURCES) == {
        "ceskereality", "realitymix", "remax", "maxima"}


# --- end to end ---------------------------------------------------------------


def _world() -> tuple[_Conn, _Store]:
    listings = [
        _listing(id=1, source="ceskereality", source_id_native="1234567",
                 source_url=_CK_URL, area_m2=870.0, area_basis="plot",
                 estate_area=870.0, property_id=11),
        _listing(id=2, source="remax", source_id_native="445483", source_url=_RM_URL,
                 area_m2=800.0, area_basis="plot", estate_area=800.0, property_id=22),
        # no archived body: reported, never written.
        _listing(id=3, source="ceskereality", source_id_native="9999999",
                 source_url=_CK_URL, area_m2=41.0, area_basis="usable"),
    ]
    conn = _Conn(listings,
                 payloads={("ceskereality", "1234567"): 101, ("remax", "445483"): 102},
                 bodies={101: "k/ck", 102: "k/rm"})
    return conn, _Store({"k/ck": CK_BODY, "k/rm": RM_BODY})


def test_a_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--dry-run")
    assert conn.updates == []
    assert conn.listings[0]["area_m2"] == 870.0
    # it still READ the bodies — the report is what it would change, not a guess.
    assert sorted(store.reads) == ["k/ck", "k/rm"]


def test_the_write_heals_the_truncated_parcel(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    healed = conn.listings[0]
    assert (healed["area_m2"], healed["area_basis"]) == (5870.0, "plot")
    assert healed["estate_area"] == 5870.0
    # every batch runs inside one transaction, so its SET LOCAL guards bind.
    assert conn.transactions == 1
    assert conn.guards == list(mod._BATCH_GUARDS)


def test_a_parcel_over_the_ceiling_becomes_a_null_headline(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`area_m2` is numeric(7,1): 16,809,800 m2 cannot be stored. The one rule declines the
    measure rather than stamping a basis for a value the row will not hold, and the SAME
    `sane_listing_numerics` the ingest path runs keeps it out of the write."""
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    row = conn.listings[1]
    assert row["area_m2"] is None and row["area_basis"] is None
    assert row["estate_area"] == 16_809_800.0
    assert row["estate_area"] > MAX_AREA_M2  # numeric(9,1) holds what (7,1) cannot


def test_a_missing_body_is_reported_never_written(
        monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    assert conn.listings[2]["area_m2"] == 41.0
    assert 3 not in conn.updates[0]["ids"]


def test_a_second_pass_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    first = [dict(r) for r in conn.listings]
    conn.updates.clear()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    assert conn.updates == []
    assert [dict(r) for r in conn.listings] == first


def test_an_unset_r2_store_refuses_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bodies live in the bucket (threshold 2 KB since migration 406). A run without a
    store would find essentially every payload spilled and report coverage over nothing."""
    from location_data import payloads as payloads_mod

    conn, _store = _world()
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://fake")
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(payloads_mod, "open_store", lambda: None)
    monkeypatch.setattr("sys.argv", ["backfill_area_spaced_thousands", "--dry-run"])
    assert mod.main() == 2
    assert conn.updates == []
