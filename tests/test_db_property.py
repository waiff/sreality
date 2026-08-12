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
    engine's job now. Property linkage keys on the SURROGATE listings.id (resolved
    from the always-present sreality_id on the sreality path), never on sreality_id.
    """
    _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT id FROM listings WHERE sreality_id" in s, [(8001,)]),  # resolve surrogate
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(None,)]),
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
    # Keyed on the surrogate (8001), NOT the sreality_id (555).
    assert link is not None and link[1] == (42, 8001)
    # the removed geo matcher: no probe, no rollup, no candidate
    assert _find(conn.executed, "SELECT price_czk, area_m2 FROM listings") is None
    assert _find(conn.executed, "SELECT p.id FROM properties p") is None
    assert _find(conn.executed, "UPDATE properties p SET") is None
    assert _find(conn.executed, "property_identity_candidates") is None


def test_linked_listing_refreshes_via_rollup(monkeypatch):
    _stub_upsert(monkeypatch, "updated")
    conn = _FakeConn([
        (lambda s: "SELECT id FROM listings WHERE sreality_id" in s, [(8002,)]),  # resolve surrogate
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(7,)]),  # already linked
    ])

    result = db.upsert_listing_with_property(conn, {"sreality_id": 777}, {}, "h")

    assert result == "updated"
    roll = _find(conn.executed, "UPDATE properties p SET")
    assert roll is not None
    # The singleton rollup keeps the display payload in sync on re-fetch, keyed on
    # the surrogate (l.id = 8002).
    assert "locality" in roll[0] and "condition" in roll[0]
    assert roll[1] == (8002,)
    assert _find(conn.executed, "INSERT INTO properties") is None
    assert _find(conn.executed, "SELECT p.id FROM properties p") is None


# --- ingest_scraped_listing (non-sreality path) ---------------------------


def _listing(**kw: Any) -> ScrapedListing:
    base = dict(source="bazos", source_id_native="218865547",
                source_url="https://bazos.cz/x", price_czk=20000, area_m2=50.0)
    base.update(kw)
    return ScrapedListing(**base)


def test_ingest_first_sight_returns_surrogate_not_synthetic(monkeypatch):
    """First sight draws a synthetic negative for the legacy sreality_id COLUMN,
    but every follow-up write (source_url, property link) — and the return value —
    keys on the SURROGATE listings.id resolved from the natural key. Post-Gate-2
    the row's sreality_id is NULL, so anything keyed on it would hit the wrong row
    or no row; this pins the identity onto the surrogate."""
    rows = _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, []),  # unseen
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-1,)]),
        (lambda s: "SELECT id FROM listings WHERE source" in s, [(8001,)]),  # surrogate, post-upsert
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    listing_id, result = db.ingest_scraped_listing(conn, _listing())

    assert listing_id == 8001 and result == "new"   # the SURROGATE, not -1
    assert _find(conn.executed, "nextval('synthetic_listing_id_seq')") is not None
    # The FULL natural key (source + native id) is carried into the INSERT row so it is
    # stamped atomically (source's column default is 'sreality', so inserting only the
    # native id could collide with a real sreality row on the unique natural-key index).
    assert rows and rows[0]["source"] == "bazos"
    assert rows and rows[0]["source_id_native"] == "218865547"
    # The legacy sreality_id column still gets the synthetic negative (pre-flip rail).
    assert rows and rows[0]["sreality_id"] == -1
    # source_url UPDATE keys on the surrogate id (8001), not the synthetic sreality_id.
    src = _find(conn.executed, "UPDATE listings SET source_url =")
    assert src is not None and src[1] == ("https://bazos.cz/x", 8001)
    assert _find(conn.executed, "UPDATE listings SET source =") is None
    assert _find(conn.executed, "INSERT INTO properties") is not None


def test_ingest_reuses_surrogate_on_refetch(monkeypatch):
    _stub_upsert(monkeypatch, "unchanged")
    conn = _FakeConn([
        # seen: the pre-lookup returns (surrogate id, legacy sreality_id)
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, [(8003, -5)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(3,)]),  # already linked
    ])

    listing_id, result = db.ingest_scraped_listing(conn, _listing())

    assert listing_id == 8003 and result == "unchanged"   # persisted surrogate reused
    assert _find(conn.executed, "nextval(") is None       # no new id drawn on refetch
    # no post-upsert re-resolve either — the surrogate was already in hand
    assert _find(conn.executed, "SELECT id FROM listings WHERE source") is None
    assert _find(conn.executed, "UPDATE properties p SET") is not None  # rollup


def test_ingest_first_sight_null_sreality_id_when_flip_enabled(monkeypatch):
    """Gate-2 flip-writer scaffold: when `gate2_null_sreality_id_enabled` reads
    true from app_settings, first sight skips the synthetic-negative sequence
    entirely and writes NULL into the legacy sreality_id column instead."""
    rows = _stub_upsert(monkeypatch)
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, []),  # unseen
        (lambda s: "SELECT value FROM app_settings WHERE key" in s, [(True,)]),
        (lambda s: "SELECT id FROM listings WHERE source" in s, [(8005,)]),  # surrogate, post-upsert
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(51,)]),
    ])

    listing_id, result = db.ingest_scraped_listing(conn, _listing())

    assert listing_id == 8005 and result == "new"
    assert _find(conn.executed, "nextval('synthetic_listing_id_seq')") is None
    assert rows and rows[0]["sreality_id"] is None


def test_ingest_survives_null_sreality_id_on_refetch(monkeypatch):
    """Post-Gate-2 a re-fetched portal row carries sreality_id = NULL. The pre-lookup
    now selects `id` (never `int(sreality_id)`), so this no longer raises
    TypeError('int(None)') on the very first refetch of every Gate-2-era row."""
    _stub_upsert(monkeypatch, "updated")
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, [(8004, None)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(9,)]),
    ])

    listing_id, result = db.ingest_scraped_listing(conn, _listing())

    assert listing_id == 8004 and result == "updated"
    # the legacy NULL flowed into to_row untouched (upsert ignores it on conflict)
    assert _find(conn.executed, "nextval(") is None


# --- broker work enqueue (the incremental resolver's sole feed) ------------


def test_ingest_enqueues_broker_work_for_idnes(monkeypatch):
    """A content-changed idnes write enqueues dirty_broker_listings so the
    incremental resolver re-attributes it within its cadence — the queue is the
    resolver's sole feed (there is no straggler scan). Mirrors the enqueue
    write_detail_batch does for sreality. Keyed on the surrogate (single-column)."""
    _stub_upsert(monkeypatch, "new")
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, []),  # unseen
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-9,)]),
        (lambda s: "SELECT id FROM listings WHERE source" in s, [(8009,)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    listing_id, result = db.ingest_scraped_listing(conn, _listing(source="idnes"))

    assert listing_id == 8009 and result == "new"
    enq = _find(conn.executed, "INSERT INTO dirty_broker_listings")
    # single-column (listing_id) INSERT, carrying the surrogate — no sreality_id join.
    assert enq is not None and enq[1] == (8009,)


def test_ingest_skips_broker_enqueue_for_non_broker_source(monkeypatch):
    """Sources the resolver doesn't attribute (bazos/bezrealitky/remax/...) never
    enter the broker queue — keeps the queue and the run metrics clean."""
    _stub_upsert(monkeypatch, "new")
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, []),
        (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-9,)]),
        (lambda s: "SELECT id FROM listings WHERE source" in s, [(8009,)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(None,)]),
        (lambda s: "INSERT INTO properties" in s, [(50,)]),
    ])

    db.ingest_scraped_listing(conn, _listing(source="bazos"))

    assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is None


def test_ingest_skips_broker_enqueue_when_unchanged(monkeypatch):
    """An unchanged re-fetch produces no snapshot, so it must not re-enqueue
    broker work — the resolver already attributed it (no churn)."""
    _stub_upsert(monkeypatch, "unchanged")
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, [(8009, -9)]),  # seen
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(3,)]),  # linked
    ])

    db.ingest_scraped_listing(conn, _listing(source="idnes"))

    assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is None


# --- broker-only changes on the HTML portals (content hash can't see them) ---


def _broker_conn(stored: dict[str, Any] | None) -> _FakeConn:
    """A seen listing whose stored raw_json->'broker' block is `stored`."""
    return _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, [(8009, -9)]),
        (lambda s: "SELECT raw_json->'broker' FROM listings WHERE id" in s, [(stored,)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(3,)]),
    ])


def test_ingest_enqueues_broker_work_when_only_the_broker_block_changed(monkeypatch):
    """The regression this exists for: the four HTML portals hash a fixed field
    allowlist (ScrapedListing._HASH_FIELDS) that excludes raw_json, so a page whose
    ONLY change is its broker block computes result == 'unchanged' and the
    result-gated enqueue never fired — those portals could not re-attribute a broker
    change at all. The check must be INDEPENDENT of the content hash."""
    _stub_upsert(monkeypatch, "unchanged")
    conn = _broker_conn({"account_oid": "aaa", "name": "Jan Novák"})

    db.ingest_scraped_listing(conn, _listing(
        source="idnes", raw={"broker": {"account_oid": "bbb", "name": "Petr Svoboda"}}))

    enq = _find(conn.executed, "INSERT INTO dirty_broker_listings")
    assert enq is not None and enq[1] == (8009,)


def test_ingest_detects_a_broker_change_on_every_html_portal_key(monkeypatch):
    """Each portal keys its broker block differently (account_oid on idnes,
    broker_id elsewhere) and carries a different firm key (agency_name /
    agency_slug / agency_id). A fingerprint covering only one portal's shape would
    silently no-op on the other three."""
    cases = [
        ("idnes", {"account_oid": "a"}, {"account_oid": "b"}),
        ("remax", {"broker_id": "1", "email": "a@x.cz"}, {"broker_id": "1", "email": "b@x.cz"}),
        ("ceskereality", {"broker_id": "1", "phone": "111"}, {"broker_id": "1", "phone": "222"}),
        ("realitymix", {"broker_id": "1", "agency_id": "7"}, {"broker_id": "1", "agency_id": "8"}),
        ("idnes", {"account_oid": "a", "agency_name": "X"},
         {"account_oid": "a", "agency_name": "Y"}),
    ]
    for source, stored, incoming in cases:
        _stub_upsert(monkeypatch, "unchanged")
        conn = _broker_conn(stored)
        db.ingest_scraped_listing(conn, _listing(source=source, raw={"broker": incoming}))
        assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is not None, source


def test_ingest_does_not_enqueue_when_the_broker_block_is_identical(monkeypatch):
    """Whitespace-only drift must not churn the queue every drain — the resolver
    would re-attribute the whole HTML corpus on every refetch."""
    _stub_upsert(monkeypatch, "unchanged")
    conn = _broker_conn({"account_oid": "aaa", "name": "Jan Novák", "email": None})

    db.ingest_scraped_listing(conn, _listing(
        source="idnes", raw={"broker": {"account_oid": " aaa ", "name": "Jan Novák "}}))

    assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is None


def test_ingest_enqueues_once_when_content_and_broker_both_changed(monkeypatch):
    """The two arms share ONE enqueue site; a second INSERT would be pure churn."""
    _stub_upsert(monkeypatch, "updated")
    conn = _broker_conn({"account_oid": "aaa"})

    db.ingest_scraped_listing(conn, _listing(
        source="idnes", raw={"broker": {"account_oid": "bbb"}}))

    enqueues = [e for e in conn.executed if "INSERT INTO dirty_broker_listings" in e[0]]
    assert len(enqueues) == 1


def test_ingest_never_reads_the_broker_block_for_an_unattributed_source(monkeypatch):
    """The read is a raw_json detoast on the live drain path. bazos/bezrealitky/
    mmreality/maxima have no broker to attribute, so they must not pay for it."""
    _stub_upsert(monkeypatch, "unchanged")
    conn = _broker_conn({"account_oid": "aaa"})

    db.ingest_scraped_listing(conn, _listing(source="bazos", raw={"broker": {"x": 1}}))

    assert _find(conn.executed, "SELECT raw_json->'broker'") is None
    assert _find(conn.executed, "INSERT INTO dirty_broker_listings") is None


def test_broker_fingerprint_survives_a_malformed_block():
    """This runs inside the live ingest transaction: a portal that emits a list, a
    scalar or nothing must degrade to daily-sweep-only attribution, never abort the
    ingestion of an otherwise-valid listing."""
    assert db._broker_fingerprint(None) == ()
    assert db._broker_fingerprint([{"name": "x"}]) == ()
    assert db._broker_fingerprint("broker") == ()
    # ...and a block whose values are not strings still fingerprints. Indexed by
    # key, not position: the allowlist is now derived from the source registry, so
    # a new portal can legitimately shift the tuple's order.
    at = db._BROKER_FINGERPRINT_KEYS.index("broker_id")
    assert db._broker_fingerprint({"broker_id": 17})[at] == "17"


def test_broker_fields_stay_out_of_the_content_hash():
    """Rule 2: listing_snapshots is for CONTENT changes. Folding the broker block
    into the hash would append a snapshot row per pure attribution change — the
    reason this enqueue is an independent check and not an extra _HASH_FIELDS entry."""
    from scraper.scraped_listing import _HASH_FIELDS, ScrapedListing

    assert not {f for f in _HASH_FIELDS if "broker" in f or f == "raw"}
    a = ScrapedListing(source="idnes", source_id_native="1", source_url="u",
                       raw={"broker": {"account_oid": "a"}})
    b = ScrapedListing(source="idnes", source_id_native="1", source_url="u",
                       raw={"broker": {"account_oid": "b"}})
    assert a.content_hash() == b.content_hash()


# --- property-stats work enqueue (the incremental recompute's ingest feed) ---


def test_ingest_enqueues_property_stats_work(monkeypatch):
    """A content-changed write enqueues dirty_properties so the */5 incremental
    recompute refreshes the price-history columns.

    This is the counterpart of write_detail_batch's _BATCH_DIRTY_FROM_SIDS_SQL,
    which only ever covered sreality. The other eight portals ingest through
    ingest_scraped_listing, so before this they never enqueued on a content
    change and their price_change_count* / total_price_change_pct were refreshed
    only by the 04:15 full sweep — while _cheap_property_rollup updates
    current_price_czk inline. A displayed price could sit up to 24h out of step
    with its own change history.
    """
    _stub_upsert(monkeypatch, "changed")
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, [(8009, -9)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(3,)]),  # linked
    ])

    db.ingest_scraped_listing(conn, _listing(source="bazos"))

    enq = _find(conn.executed, "INSERT INTO dirty_properties")
    # Keyed on the SURROGATE listings.id; the property is resolved inside the
    # statement so a listing with property_id NULL is skipped, not crashed on.
    assert enq is not None and enq[1] == (8009,)
    assert "property_id IS NOT NULL" in enq[0]


def test_ingest_enqueues_property_stats_work_for_every_source(monkeypatch):
    """Unlike the broker queue, the stats queue is source-independent — a price
    change matters for the rollup on all nine portals."""
    for source in ("idnes", "bazos", "remax", "bezrealitky"):
        _stub_upsert(monkeypatch, "new")
        conn = _FakeConn([
            (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, []),
            (lambda s: "SELECT nextval('synthetic_listing_id_seq')" in s, [(-9,)]),
            (lambda s: "SELECT id FROM listings WHERE source" in s, [(8009,)]),
            (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(None,)]),
            (lambda s: "INSERT INTO properties" in s, [(50,)]),
        ])

        db.ingest_scraped_listing(conn, _listing(source=source))

        assert _find(conn.executed, "INSERT INTO dirty_properties") is not None, source


def test_ingest_skips_property_stats_enqueue_when_unchanged(monkeypatch):
    """An unchanged re-fetch appends no snapshot, so nothing about the price
    history moved — re-enqueueing would churn the queue on every drain cycle."""
    _stub_upsert(monkeypatch, "unchanged")
    conn = _FakeConn([
        (lambda s: "SELECT id, sreality_id FROM listings WHERE source" in s, [(8009, -9)]),
        (lambda s: "SELECT property_id FROM listings WHERE id" in s, [(3,)]),
    ])

    db.ingest_scraped_listing(conn, _listing(source="idnes"))

    assert _find(conn.executed, "INSERT INTO dirty_properties") is None


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


def test_scraped_listing_to_row_accepts_none_sreality_id():
    """Gate-2 flip-writer scaffold: to_row's signature is widened to `int | None`
    so a flag-on first-sight write can pass NULL straight through."""
    row = _listing().to_row(None)
    assert row["sreality_id"] is None


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
