"""Tests for the property linkage (scraper.db).

Hermetic: `upsert_listing` is stubbed so the only SQL reaching the fake conn
comes from the property linkage. A new (unlinked) listing always becomes its
own singleton property — the old geo Tier-1 spatial probe was removed when
matching moved to the out-of-band street+disposition dedup engine. The fake
conn matches each executed statement against a scripted (predicate -> rows)
list and records every execution so the test can assert what linkage emitted.
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


def _stub_upsert(monkeypatch, result: str = "new") -> list[dict[str, Any]]:
    """Stub upsert_listing and capture each row it was handed, so callers can
    assert what the ingest path put into the INSERT row (e.g. source_id_native)."""
    rows: list[dict[str, Any]] = []

    def _fake(_conn: Any, row: dict[str, Any], *a: Any, **k: Any) -> str:
        rows.append(row)
        return result

    monkeypatch.setattr(db, "upsert_listing", _fake)
    return rows


def _find(executions, needle: str) -> tuple[str, Any] | None:
    return next((e for e in executions if needle in e[0]), None)


# --- property linkage branches (via upsert_listing_with_property) ---------


def test_new_listing_creates_singleton(monkeypatch):
    """A new (unlinked) listing always becomes its own singleton property.

    No geo probe, no candidate enqueue — matching is the out-of-band dedup
    engine's job now.
    """
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(42,)]),
    ])

    result = db.upsert_listing_with_property(conn, {"sreality_id": 555}, {}, "h")

    assert result == "new"
    ins = _find(conn.executed, "INSERT INTO properties")
    assert ins is not None
    # The singleton must carry the FULL display payload at creation, not just
    # structural columns — otherwise the Browse card has no city/condition until
    # the next full recompute (portal inserts never enter the dirty drain, so
    # that's up to ~24h). Guards against the column list being trimmed again.
    assert "locality" in ins[0] and "condition" in ins[0]
    link = _find(conn.executed, "UPDATE listings SET property_id =")
    assert link is not None and link[1] == (42, 555)
    # the removed geo matcher: no probe, no rollup, no candidate
    assert _find(conn.executed, "SELECT price_czk, area_m2 FROM listings") is None
    assert _find(conn.executed, "SELECT p.id FROM properties p") is None
    assert _find(conn.executed, "UPDATE properties p SET") is None
    assert _find(conn.executed, "property_identity_candidates") is None


def test_linked_listing_refreshes_via_rollup(monkeypatch):
    _stub_upsert(monkeypatch, "updated")
    conn = _FakeConn([
        (lambda s: "SELECT property_id FROM listings" in s, [(7,)]),  # already linked
    ])

    result = db.upsert_listing_with_property(conn, {"sreality_id": 777}, {}, "h")

    assert result == "updated"
    roll = _find(conn.executed, "UPDATE properties p SET")
    assert roll is not None
    # The singleton rollup keeps the display payload in sync on re-fetch.
    assert "locality" in roll[0] and "condition" in roll[0]
    assert _find(conn.executed, "INSERT INTO properties") is None
    assert _find(conn.executed, "SELECT p.id FROM properties p") is None


# --- ingest_scraped_listing (non-sreality path) ---------------------------


def _listing(**kw: Any) -> ScrapedListing:
    base = dict(source="bazos", source_id_native="218865547",
                source_url="https://bazos.cz/x", price_czk=20000, area_m2=50.0)
    base.update(kw)
    return ScrapedListing(**base)


def test_ingest_first_sight_draws_synthetic_pk(monkeypatch):
    rows = _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT sreality_id FROM listings WHERE source" in s, []),  # unseen
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-1,)]),
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    pk, result = db.ingest_scraped_listing(conn, _listing())

    assert pk == -1 and result == "new"
    assert _find(conn.executed, "nextval('synthetic_listing_id_seq')") is not None
    # The FULL natural key (source + native id) is carried into the INSERT row so it is
    # stamped atomically (source's column default is 'sreality', so inserting only the
    # native id could collide with a real sreality row on the unique natural-key index).
    # Only source_url — not part of the key — remains on the post-insert UPDATE.
    assert rows and rows[0]["source"] == "bazos"
    assert rows and rows[0]["source_id_native"] == "218865547"
    src = _find(conn.executed, "UPDATE listings SET source_url =")
    assert src is not None and src[1] == ("https://bazos.cz/x", -1)
    assert _find(conn.executed, "UPDATE listings SET source =") is None
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


# --- broker work enqueue (the incremental resolver's sole feed) ------------


def test_ingest_enqueues_broker_work_for_idnes(monkeypatch):
    """A content-changed idnes write enqueues dirty_broker_listings so the
    incremental resolver re-attributes it within its cadence — the queue is the
    resolver's sole feed (there is no straggler scan). Mirrors the enqueue
    write_detail_batch does for sreality."""
    _stub_upsert(monkeypatch, "new")
    conn = _FakeConn([
        (lambda s: "SELECT sreality_id FROM listings WHERE source" in s, []),  # unseen
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-9,)]),
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    pk, result = db.ingest_scraped_listing(conn, _listing(source="idnes"))

    assert pk == -9 and result == "new"
    enq = _find(conn.executed, "INSERT INTO dirty_broker_listings")
    assert enq is not None and enq[1] == (-9,)


def test_ingest_skips_broker_enqueue_for_non_broker_source(monkeypatch):
    """Sources the resolver doesn't attribute (bazos/bezrealitky/remax/...) never
    enter the broker queue — keeps the queue and the run metrics clean."""
    _stub_upsert(monkeypatch, "new")
    conn = _FakeConn([
        (lambda s: "SELECT sreality_id FROM listings WHERE source" in s, []),
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-9,)]),
        (lambda s: "SELECT property_id FROM listings" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    db.ingest_scraped_listing(conn, _listing(source="bazos"))

    assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is None


def test_ingest_skips_broker_enqueue_when_unchanged(monkeypatch):
    """An unchanged re-fetch produces no snapshot, so it must not re-enqueue
    broker work — the resolver already attributed it (no churn)."""
    _stub_upsert(monkeypatch, "unchanged")
    conn = _FakeConn([
        (lambda s: "SELECT sreality_id FROM listings WHERE source" in s, [(-9,)]),  # seen
        (lambda s: "SELECT property_id FROM listings" in s, [(3,)]),  # linked
    ])

    db.ingest_scraped_listing(conn, _listing(source="idnes"))

    assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is None


# --- ScrapedListing contract ----------------------------------------------


def test_scraped_listing_content_hash_is_stable_and_price_sensitive():
    a = _listing()
    assert a.content_hash() == _listing().content_hash()
    assert a.content_hash() != _listing(price_czk=21000).content_hash()
    assert a.content_hash() != _listing(description="nový popis").content_hash()
    # source identity is NOT part of the content hash
    assert a.content_hash() == _listing(source_url="https://bazos.cz/other").content_hash()
    # lat/lon are derived/geocoded, oscillation-prone, and geom updates on
    # every upsert anyway — a coords-only change must NOT spawn a snapshot
    assert a.content_hash() == _listing(lat=50.0, lon=14.4).content_hash()


def test_scraped_listing_to_row_maps_fields():
    row = _listing(disposition="2+kk", lat=50.0, lon=14.4).to_row(-7)
    assert row["sreality_id"] == -7
    assert row["lat"] == 50.0 and row["lon"] == 14.4
    assert row["disposition"] == "2+kk"
    assert row["price_czk"] == 20000
    # sreality-only locality ids aren't carried; upsert_listing defaults them.
    assert "locality_district_id" not in row
