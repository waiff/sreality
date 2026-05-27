"""Tests for the property linkage + Tier-1 matcher (scraper.db).

Hermetic: `upsert_listing` is stubbed so the only SQL reaching the fake conn
comes from the property linkage / matcher. The fake conn matches each executed
statement against a scripted list of (predicate -> rows) pairs and records
every execution so the test can assert what the matcher emitted. The spatial
SQL itself (ST_DWithin, same-source exclusion) is verified out-of-band via the
Supabase MCP.
"""

from __future__ import annotations

from typing import Any

from scraper import db
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


def _stub_upsert(monkeypatch, result: str = "new") -> None:
    monkeypatch.setattr(db, "upsert_listing", lambda *a, **k: result)


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


def _find_all(executions, needle: str) -> list[tuple[str, Any]]:
    return [e for e in executions if needle in e[0]]


# --- Tier-1 matcher branches (via upsert_listing_with_property) -----------


def test_new_listing_without_geo_key_creates_singleton(monkeypatch):
    """No price/area -> spatial probe is skipped entirely -> singleton."""
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "SELECT price_czk, area_m2 FROM listings" in s, [(None, None)]),
        (lambda s: "INSERT INTO properties" in s, [(42,)]),
    ])

    result = db.upsert_listing_with_property(conn, {"sreality_id": 555}, {}, "h")

    assert result == "new"
    assert _find(conn.executed, "INSERT INTO properties") is not None
    link = _find(conn.executed, "UPDATE listings SET property_id =")
    assert link is not None and link[1] == (42, 555)
    # probe never ran (no key); no attach rollup; no candidate
    assert _find(conn.executed, "SELECT p.id FROM properties p") is None
    assert _find(conn.executed, "UPDATE properties p SET") is None
    assert _find(conn.executed, "property_identity_candidates") is None


def test_new_listing_zero_hits_creates_singleton(monkeypatch):
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "SELECT price_czk, area_m2 FROM listings" in s, [(20000, 50.0)]),
        (lambda s: "SELECT p.id FROM properties p" in s, []),  # zero hits
        (lambda s: "INSERT INTO properties" in s, [(42,)]),
    ])

    db.upsert_listing_with_property(conn, {"sreality_id": 555}, {}, "h")

    assert _find(conn.executed, "SELECT p.id FROM properties p") is not None
    assert _find(conn.executed, "INSERT INTO properties") is not None
    assert _find(conn.executed, "property_identity_candidates") is None


def test_new_listing_unique_hit_attaches(monkeypatch):
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "SELECT price_czk, area_m2 FROM listings" in s, [(20000, 50.0)]),
        (lambda s: "SELECT p.id FROM properties p" in s, [(7,)]),  # one hit
    ])

    db.upsert_listing_with_property(conn, {"sreality_id": 555}, {}, "h")

    # Attached to property 7, no new property created, rollup ran.
    link = _find(conn.executed, "UPDATE listings SET property_id =")
    assert link is not None and link[1] == (7, 555)
    assert _find(conn.executed, "INSERT INTO properties") is None
    assert _find(conn.executed, "UPDATE properties p SET") is not None
    assert _find(conn.executed, "property_identity_candidates") is None


def test_new_listing_multi_hit_enqueues_candidates(monkeypatch):
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "SELECT price_czk, area_m2 FROM listings" in s, [(20000, 50.0)]),
        (lambda s: "SELECT p.id FROM properties p" in s, [(7,), (9,)]),  # ambiguous
        (lambda s: "INSERT INTO properties" in s, [(42,)]),
    ])

    db.upsert_listing_with_property(conn, {"sreality_id": 555}, {}, "h")

    # New singleton + a candidate row per hit, ordered (left < right).
    assert _find(conn.executed, "INSERT INTO properties") is not None
    cands = _find_all(conn.executed, "INSERT INTO property_identity_candidates")
    assert len(cands) == 2
    pairs = {(c[1][0], c[1][1]) for c in cands}
    assert pairs == {(7, 42), (9, 42)}


def test_linked_listing_refreshes_via_rollup(monkeypatch):
    _stub_upsert(monkeypatch, "updated")
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(7,)]),  # already linked
    ])

    result = db.upsert_listing_with_property(conn, {"sreality_id": 777}, {}, "h")

    assert result == "updated"
    assert _find(conn.executed, "UPDATE properties p SET") is not None
    assert _find(conn.executed, "INSERT INTO properties") is None
    assert _find(conn.executed, "SELECT p.id FROM properties p") is None


# --- ingest_scraped_listing (non-sreality path) ---------------------------


def _listing(**kw: Any) -> ScrapedListing:
    base = dict(source="bazos", source_id_native="218865547",
                source_url="https://bazos.cz/x", price_czk=20000, area_m2=50.0)
    base.update(kw)
    return ScrapedListing(**base)


def test_ingest_first_sight_draws_synthetic_pk(monkeypatch):
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT sreality_id FROM listings WHERE source" in s, []),  # unseen
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-1,)]),
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "SELECT price_czk, area_m2 FROM listings" in s, [(20000, 50.0)]),
        (lambda s: "SELECT p.id FROM properties p" in s, []),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    pk, result = db.ingest_scraped_listing(conn, _listing())

    assert pk == -1 and result == "new"
    assert _find(conn.executed, "nextval('synthetic_listing_id_seq')") is not None
    src = _find(conn.executed, "UPDATE listings SET source =")
    assert src is not None and src[1] == ("bazos", "https://bazos.cz/x", "218865547", -1)
    assert _find(conn.executed, "INSERT INTO properties") is not None


def test_ingest_reuses_pk_on_refetch(monkeypatch):
    _stub_upsert(monkeypatch, "unchanged")
    conn = _FakeConn([
        (lambda s: "SELECT sreality_id FROM listings WHERE source" in s, [(-5,)]),  # seen
        (lambda s: "SELECT property_id FROM listings" in s, [(3,)]),  # already linked
    ])

    pk, result = db.ingest_scraped_listing(conn, _listing())

    assert pk == -5 and result == "unchanged"
    assert _find(conn.executed, "nextval(") is None  # no new PK drawn
    assert _find(conn.executed, "UPDATE properties p SET") is not None  # rollup


# --- ScrapedListing contract ----------------------------------------------


def test_scraped_listing_content_hash_is_stable_and_price_sensitive():
    a = _listing()
    assert a.content_hash() == _listing().content_hash()
    assert a.content_hash() != _listing(price_czk=21000).content_hash()
    # source identity is NOT part of the content hash
    assert a.content_hash() == _listing(source_url="https://bazos.cz/other").content_hash()


def test_scraped_listing_to_row_maps_fields():
    row = _listing(disposition="2+kk", lat=50.0, lon=14.4).to_row(-7)
    assert row["sreality_id"] == -7
    assert row["lat"] == 50.0 and row["lon"] == 14.4
    assert row["disposition"] == "2+kk"
    assert row["price_czk"] == 20000
    # sreality-only locality ids aren't carried; upsert_listing defaults them.
    assert "locality_district_id" not in row
