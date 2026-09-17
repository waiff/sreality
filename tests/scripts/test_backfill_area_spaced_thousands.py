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

from datetime import datetime, timedelta, timezone
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
               "estate_area", "garden_area", "area_basis")


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
        if sql == mod._STATEMENT_TIMEOUT_SQL:
            self._conn.armed_statement_timeout = True
        elif "GROUP BY source" in sql:
            self._rows = [(s, 1, 1, 1) for s in sorted(
                {r["source"] for r in self._conn.listings})]
        elif "portal_raw_payloads p" in sql:
            self._rows = [(s, n, pid, self._conn.observed_at.get(pid))
                          for (s, n), pid in sorted(self._conn.payloads.items())
                          if n in params["natives"] and s == params["source"]]
        elif "FROM portal_raw_payloads" in sql:
            # `page_readers.load_bodies` own statement: id, body, body_r2_key, encoding.
            self._rows = [(pid, None, key, "identity")
                          for pid, key in sorted(self._conn.bodies.items())
                          if pid in params["ids"]]
        elif "FROM listing_snapshots" in sql:
            self._rows = [(lid, at) for lid, at
                          in sorted(self._conn.snapshot_at.items())
                          if lid in params["ids"]]
        elif sql.strip().startswith("SELECT id"):
            rows = sorted((r for r in self._conn.listings
                           if r["id"] > params["after"]
                           and r["source"] == params["source"]),
                          key=lambda r: r["id"])
            self._rows = [tuple(r[c] for c in _PROJECTION)
                          for r in rows[:params["page"]]]
        elif sql.startswith("SET LOCAL "):
            self._conn.guards.append(sql)
        elif "UPDATE listings" in sql and "dirty_properties" in sql:
            self._conn.updates.append(params)
            by_id = {r["id"]: r for r in self._conn.listings}
            for i, lid in enumerate(params["ids"]):
                row = by_id[lid]
                for col in (*mod._AREA_COLS, "area_basis"):
                    value = params[col][i]
                    # numeric(*,1) on every area column: the row reads back at scale 1,
                    # which is what makes a second pass comparable at all.
                    row[col] = round(value, 1) if isinstance(value, float) else value
                if row["property_id"] is not None:
                    self._conn.dirty.add(row["property_id"])
            self.rowcount = len(self._conn.dirty)
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
                 bodies: dict[int, str], *,
                 observed_at: dict[int, Any] | None = None,
                 snapshot_at: dict[int, Any] | None = None) -> None:
        self.listings = listings
        self.payloads = payloads
        self.bodies = bodies
        self.observed_at = observed_at or {}
        self.snapshot_at = snapshot_at or {}
        self.executed: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self.guards: list[str] = []
        self.dirty: set[int] = set()
        self.transactions = 0
        self.armed_statement_timeout = False

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


def test_the_page_read_is_one_source_at_a_time() -> None:
    """The paging SELECT measured 40.5 s against the cluster's 120 s default. A page that
    filtered `source = ANY(...)` stepped over every other portal's rows to find its own."""
    sql = " ".join(mod._SELECT_SQL.split())
    assert "source = %(source)s" in sql and "ANY(%(sources)s" not in sql
    assert "id > %(after)s::bigint" in sql and "ORDER BY id" in sql
    assert "source = %(source)s" in " ".join(mod._PAYLOADS_SQL.split())


def test_the_payload_read_takes_the_latest_successful_detail_body() -> None:
    sql = " ".join(mod._PAYLOADS_SQL.split())
    assert "DISTINCT ON (p.source, p.source_id_native)" in sql
    assert "p.page_kind = 'detail'" in sql
    assert "p.version_seq DESC NULLS LAST" in sql
    # NULL ranks as successful, exactly as location_data.payloads ranks it.
    assert "p.http_status IS NULL OR p.http_status BETWEEN 200 AND 299" in sql
    # and it carries the clock the staleness gate compares against.
    assert "p.last_observed_at" in mod._PAYLOADS_SQL.split("FROM")[0]


def test_the_write_touches_areas_only_and_enqueues_in_the_same_statement() -> None:
    sql = " ".join(mod._UPDATE_SQL.split())
    # one UPDATE of `listings`; the second "UPDATE" is the enqueue's upsert clause.
    assert sql.count("UPDATE listings") == 1
    assert sql.count("UPDATE ") == 2 and "ON CONFLICT (property_id) DO UPDATE" in sql
    assert "price" not in sql and "listing_snapshots" not in sql
    for col in (*mod._AREA_COLS, "area_basis"):
        assert f"{col} = u.{col}" in sql
    # rule 20: the dirty mark rides the SAME statement, so it cannot survive a rollback.
    assert "INSERT INTO dirty_properties" in sql and "RETURNING l.property_id" in sql
    assert any("statement_timeout" in g for g in mod._BATCH_GUARDS)
    assert all(g.startswith("SET LOCAL ") for g in mod._BATCH_GUARDS)


def test_bazos_is_in_the_healed_population() -> None:
    """8.4 % of the newest 8,000 bazos rows carry the exact truncation fingerprint, and its
    parser now shares the grammar — so the same re-parse applies."""
    assert set(mod.DEFAULT_SOURCES) == {
        "ceskereality", "realitymix", "remax", "maxima", "bazos"}
    assert mod._parser_for("bazos") is not None


def test_the_budget_defaults_under_the_runner_timeout() -> None:
    """~193k bodies do not fit one job, and a run cancelled by the runner's timeout stamps
    no resumable stop — so the budget is a default, not something the dispatcher has to
    remember. It must stay under the workflow's `timeout-minutes: 180`."""
    defaults = {a.dest: a.default for a in mod._build_parser()._actions}
    assert defaults["max_seconds"] == 9000.0
    assert defaults["max_seconds"] < 180 * 60
    assert defaults["dry_run"] is True


# --- end to end ---------------------------------------------------------------


_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


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
                 bodies={101: "k/ck", 102: "k/rm"},
                 observed_at={101: _NOW, 102: _NOW})
    return conn, _Store({"k/ck": CK_BODY, "k/rm": RM_BODY})


def test_a_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--dry-run")
    assert conn.updates == []
    assert conn.listings[0]["area_m2"] == 870.0
    # it still READ the bodies — the report is what it would change, not a guess.
    assert sorted(store.reads) == ["k/ck", "k/rm"]


def test_the_run_arms_its_own_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paging SELECT measured 40.5 s and the count 13.6 s against the cluster's 120 s
    default — too close to it to run unguarded, as the sibling backfills already knew."""
    conn, store = _world()
    _run(monkeypatch, conn, store, "--dry-run")
    assert conn.armed_statement_timeout
    assert conn.executed[0] == mod._STATEMENT_TIMEOUT_SQL


def test_the_write_heals_the_truncated_parcel(monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    healed = conn.listings[0]
    assert (healed["area_m2"], healed["area_basis"]) == (5870.0, "plot")
    assert healed["estate_area"] == 5870.0
    # every batch runs inside one transaction, so its SET LOCAL guards bind ...
    assert conn.transactions >= 1
    assert conn.guards[:len(mod._BATCH_GUARDS)] == list(mod._BATCH_GUARDS)
    # ... and the dirty mark rode the same statement (rule 20).
    assert conn.dirty == {11, 22}   # the remax row is healed too, below


def test_a_parcel_over_the_ceiling_never_blanks_the_stored_headline(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`area_m2` is numeric(7,1): 16,809,800 m2 cannot be stored, so the one rule declines
    the measure rather than stamping a basis for a value the row will not hold. The heal
    then leaves the headline the row already carried — writing NULL over a stored area is
    indistinguishable from a parser shape drift, and must never happen — while
    `estate_area` (numeric(9,1)) is healed beside it."""
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    row = conn.listings[1]
    assert row["estate_area"] == 16_809_800.0
    assert row["estate_area"] > MAX_AREA_M2  # numeric(9,1) holds what (7,1) cannot
    assert row["area_m2"] == 800.0 and row["area_basis"] == "plot"


def test_a_missing_body_is_reported_never_written(
        monkeypatch: pytest.MonkeyPatch) -> None:
    conn, store = _world()
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    assert conn.listings[2]["area_m2"] == 41.0
    assert all(3 not in u["ids"] for u in conn.updates)


def test_a_body_older_than_the_listings_newest_snapshot_is_skipped(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The payload writer discards a CHANGED body inside its 7-day per-listing floor, so
    ~3.2 % of rows (≈6k) have a snapshot newer than their newest archived body. Re-parsing
    one would revert the seller's edit to a week-old page."""
    conn, store = _world()
    conn.snapshot_at = {1: _NOW + timedelta(days=1)}
    _run(monkeypatch, conn, store, "--write", "--ignore-rebuild")
    assert conn.listings[0]["area_m2"] == 870.0     # untouched
    assert all(1 not in u["ids"] for u in conn.updates)
    # a snapshot OLDER than the body is not a reason to skip.
    conn2, store2 = _world()
    conn2.snapshot_at = {1: _NOW - timedelta(days=1)}
    _run(monkeypatch, conn2, store2, "--write", "--ignore-rebuild")
    assert conn2.listings[0]["area_m2"] == 5870.0


def test_a_two_decimal_area_does_not_rewrite_itself_every_pass(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Every area column is numeric(*,1), so the row reads back rounded. Comparing the
    fresh 86.19 against the stored 86.2 made the heal rewrite live rows for ever."""
    listings = [_listing(id=9, source="ceskereality", source_id_native="1234567",
                         source_url=_CK_URL, category_main="byt", area_m2=86.2,
                         area_basis="usable", usable_area=86.2, property_id=99)]
    conn = _Conn(listings, payloads={("ceskereality", "1234567"): 101},
                 bodies={101: "k/ck"}, observed_at={101: _NOW})
    body = (
        "<html><body><h1>Prodej bytu 2+kk 86,19 m\u00b2</h1>"
        '<dl class="g-info"><div class="i-info">'
        '<span class="i-info__title">Plocha u\u017eitn\u00e1</span>'
        '<span class="i-info__value">86,19 m\u00b2</span></div></dl></body></html>'
    ).encode("utf-8")
    _run(monkeypatch, conn, _Store({"k/ck": body}), "--write", "--ignore-rebuild")
    assert conn.updates == []
    assert conn.listings[0]["area_m2"] == 86.2


def test_the_column_scale_rounds_the_way_postgres_does() -> None:
    """`round()` is banker's; numeric is half-up. On an exact .x5 area the two disagree, and
    a value the column then rounds differently re-diffs on the very next pass."""
    assert mod._at_column_scale(86.25) == 86.3      # round(86.25, 1) is 86.2
    assert mod._at_column_scale(86.19) == 86.2
    assert mod._at_column_scale(None) is None
    from decimal import Decimal

    assert mod._at_column_scale(Decimal("86.2")) == 86.2


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
