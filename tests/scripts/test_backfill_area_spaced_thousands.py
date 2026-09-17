"""The spaced-thousands heal: the row's own page fields, re-read correctly, nothing else.

This job re-derives a listing's areas from `listings.raw_json` — the parser's own latest
reading of the live page — with the fixed grammar, and writes back the area columns. What
has to hold: it reads the PORTAL's own key precedence rather than a second copy of it; the
selection covers every row a truncation could have touched and carries no `raw_json`
predicate (one over it detoasts every candidate — the cost that killed
`backfill_mmreality_areas`'s first dispatch); the write touches areas only, never price and
never a snapshot; the numeric ceiling is applied by the SAME guard the ingest path uses, so
an unstorable parcel becomes NULL instead of a 22003 that aborts the page; a dry run writes
nothing; and a healed row re-derives to itself, so a second pass is a no-op.

The substrate matters and is asserted: an earlier cut re-parsed the ARCHIVED body out of
`portal_raw_payloads` + R2, which lags the live row by up to the payload writer's 7-day
floor — a gate that refused to apply a stale body skipped 89 % of the population on the
first production dry run. `raw_json` cannot lag; it is rewritten by the same transaction
that writes the areas.

The fake connection below is a small in-memory `listings`, so the write is actually applied
and the idempotence claim is exercised rather than asserted.
"""

from __future__ import annotations

from typing import Any

import pytest

from scraper.area import MAX_AREA_M2
from scripts import backfill_area_spaced_thousands as mod

NB = " "


# --- a tiny in-memory listings ------------------------------------------------


class _Row(dict):
    """One `listings` row, addressed the way the script's SELECT projects it."""


def _listing(**kw: Any) -> _Row:
    row = _Row(id=0, source="ceskereality", property_id=None, category_main="pozemek",
               area_m2=None, usable_area=None, estate_area=None, garden_area=None,
               area_basis=None, params=None, title=None, ad_text=None, is_active=True)
    row.update(kw)
    return row


_PROJECTION = ("id", "property_id", "category_main", "area_m2", "usable_area",
               "estate_area", "garden_area", "area_basis", "params", "title", "ad_text")


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
        elif sql.strip().startswith("SELECT id"):
            rows = sorted((r for r in self._conn.listings
                           if r["id"] > params["after"]
                           and r["source"] == params["source"]),
                          key=lambda r: r["id"])
            text_lane = "description AS ad_text" in sql
            self._rows = [
                tuple(None if (c == "ad_text" and not text_lane)
                      else None if (c == "params" and text_lane)
                      else r[c] for c in _PROJECTION)
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
    def __init__(self, listings: list[_Row]) -> None:
        self.listings = listings
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


def _run(monkeypatch: pytest.MonkeyPatch, conn: _Conn, *argv: str) -> None:
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://fake")
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr("sys.argv", ["backfill_area_spaced_thousands", *argv])
    assert mod.main() == 0


def _module_code() -> str:
    """The heal's source with its module docstring removed — what it DOES, not what it says."""
    source = open(mod.__file__, encoding="utf-8").read()
    return source.split('"""', 2)[2]


# --- the substrate ------------------------------------------------------------


def test_the_substrate_is_the_rows_own_page_fields_not_an_archived_body() -> None:
    """The archive lags the live row by up to the payload writer's 7-day per-listing floor,
    and a gate that refused to apply a stale body skipped 89 % of the population on the
    first production dry run (examined=3000, would change=85, body_stale=2672). `raw_json`
    is rewritten by the same transaction that writes the areas, so it cannot lag."""
    code = _module_code()
    for gone in ("portal_raw_payloads", "load_bodies", "open_store", "body_stale",
                 "listing_snapshots", "R2_", "download_bytes"):
        assert gone not in code, gone
    assert "raw_json->'params'" in mod._SELECT_SQL
    assert "raw_json->>'title'" in mod._SELECT_SQL
    # bazos has no spec table: the ad body is its substrate, on its own statement so the
    # other four never detoast `description` for nothing.
    assert "description AS ad_text" in mod._SELECT_TEXT_SQL
    assert "description" not in mod._SELECT_SQL
    assert mod.TEXT_SOURCES == frozenset({"bazos"})


def test_the_portals_own_key_precedence_is_called_never_recopied() -> None:
    """A second copy of a key order is the same defect as a second copy of the number
    grammar, one level up (rule 21). Each parser exposes the function its own
    `parse_detail` calls, and the heal calls exactly that."""
    from scraper.bazos_parser import areas_from_text
    from scraper.ceskereality_parser import areas_from_params as ck
    from scraper.maxima_parser import areas_from_params as mx
    from scraper.realitymix_parser import areas_from_params as rmix
    from scraper.remax_parser import areas_from_params as rmax

    assert all(callable(f) for f in (ck, rmix, rmax, mx, areas_from_text))
    # CODE only — the module docstring quotes a key or two as illustration.
    code = _module_code()
    for key in ("plocha pozemku", "užitná plocha", "plocha parcely",
                "celkova plocha", "plocha zahrady"):
        assert key not in code, f"{key!r} is the parser's to spell, not the heal's"


# --- the selection ------------------------------------------------------------


def test_the_suspect_predicate_is_spelled_once_and_used_everywhere() -> None:
    assert mod._SUSPECT in mod._COUNT_BY_SOURCE_SQL
    assert mod._SUSPECT in mod._SELECT_SQL and mod._SUSPECT in mod._SELECT_TEXT_SQL
    # a truncation leaves the last three digits ...
    for col in mod._AREA_COLS:
        assert f"{col} < 1000" in mod._SUSPECT
    # ... unless they were "000", which the write boundary NULLed: the all-NULL arm.
    assert "area_m2 IS NULL AND usable_area IS NULL" in " ".join(mod._SUSPECT.split())


def test_no_predicate_ever_touches_a_wide_column() -> None:
    """`raw_json` is PROJECTED per page, which is a fine read; a predicate over it would
    detoast every candidate row, the cost that killed `backfill_mmreality_areas`'s first
    live dispatch on a 120 s statement_timeout."""
    assert "raw_json" not in mod._SUSPECT and "description" not in mod._SUSPECT
    for sql in (mod._SELECT_SQL, mod._SELECT_TEXT_SQL):
        where = sql.split("WHERE", 1)[1]
        assert "raw_json" not in where and "description" not in where
    assert "raw_json" not in mod._COUNT_BY_SOURCE_SQL


def test_the_page_read_is_one_source_at_a_time() -> None:
    """The paging SELECT measured 40.5 s against the cluster's 120 s default. A page that
    filtered `source = ANY(...)` stepped over every other portal's rows to find its own."""
    for sql in (mod._SELECT_SQL, mod._SELECT_TEXT_SQL):
        flat = " ".join(sql.split())
        assert "source = %(source)s" in flat and "ANY(%(sources)s" not in flat
        assert "id > %(after)s::bigint" in flat and "ORDER BY id" in flat
        assert "property_id" in flat.split("FROM")[0]  # rule 20


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
    parser now shares the grammar — so the same re-derive applies, off its ad text."""
    assert set(mod.DEFAULT_SOURCES) == {
        "ceskereality", "realitymix", "remax", "maxima", "bazos"}


def test_the_budget_defaults_under_the_runner_timeout() -> None:
    """A run cancelled by the runner's timeout stamps no resumable stop, so the budget is a
    default rather than something the dispatcher has to remember."""
    defaults = {a.dest: a.default for a in mod._build_parser()._actions}
    assert defaults["max_seconds"] == 9000.0
    assert defaults["max_seconds"] < 180 * 60
    assert defaults["dry_run"] is True


def test_the_column_scale_rounds_the_way_postgres_does() -> None:
    """`round()` is banker's; numeric is half-up. On an exact .x5 area the two disagree, and
    a value the column then rounds differently re-diffs on the very next pass."""
    from decimal import Decimal

    assert mod._at_column_scale(86.25) == 86.3      # round(86.25, 1) is 86.2
    assert mod._at_column_scale(86.19) == 86.2
    assert mod._at_column_scale(None) is None
    assert mod._at_column_scale(Decimal("86.2")) == 86.2


# --- end to end ---------------------------------------------------------------


def _world() -> _Conn:
    return _Conn([
        # ceskereality: the parcel its page renders with a thousands group. The naive
        # grammar read 870 — which is why NOT ONE of its 19,088 land rows had an area_m2
        # of 1000 or more.
        _listing(id=1, source="ceskereality", property_id=11,
                 area_m2=870.0, area_basis="plot", estate_area=870.0,
                 params={"plocha pozemku": f"5{NB}870 m²"},
                 title=f"Prodej pozemku 5{NB}870 m²"),
        # remax: a parcel `area_m2` (numeric(7,1)) cannot store.
        _listing(id=2, source="remax", property_id=22,
                 area_m2=800.0, area_basis="plot", estate_area=800.0,
                 params={"plocha pozemku": f"16{NB}809{NB}800 m²"},
                 title=f"Prodej pozemku 16{NB}809{NB}800 m², Brno"),
        # no page fields at all: reported, never written.
        _listing(id=3, source="ceskereality", area_m2=41.0, area_basis="usable",
                 params=None, title=None),
    ])


def test_a_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _world()
    _run(monkeypatch, conn, "--dry-run")
    assert conn.updates == []
    assert conn.listings[0]["area_m2"] == 870.0


def test_the_run_arms_its_own_statement_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The paging SELECT measured 40.5 s and the count 13.6 s against the cluster's 120 s
    default — too close to it to run unguarded, as the sibling backfills already knew."""
    conn = _world()
    _run(monkeypatch, conn, "--dry-run")
    assert conn.armed_statement_timeout
    assert conn.executed[0] == mod._STATEMENT_TIMEOUT_SQL


def test_the_write_heals_the_truncated_parcel(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _world()
    _run(monkeypatch, conn, "--write", "--ignore-rebuild")
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
    indistinguishable from a shape drift, and must never happen — while `estate_area`
    (numeric(9,1)) is healed beside it."""
    conn = _world()
    _run(monkeypatch, conn, "--write", "--ignore-rebuild")
    row = conn.listings[1]
    assert row["estate_area"] == 16_809_800.0
    assert row["estate_area"] > MAX_AREA_M2  # numeric(9,1) holds what (7,1) cannot
    assert row["area_m2"] == 800.0 and row["area_basis"] == "plot"


def test_a_row_with_no_page_fields_is_reported_never_written(
        monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _world()
    _run(monkeypatch, conn, "--write", "--ignore-rebuild")
    assert conn.listings[2]["area_m2"] == 41.0
    assert all(3 not in u["ids"] for u in conn.updates)


def test_bazos_reads_its_ad_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """bazos publishes no spec table: the parcel lives in the ad's prose, which is the one
    substrate where it is always written "1 500 m2"."""
    conn = _Conn([
        _listing(id=7, source="bazos", category_main="pozemek", property_id=77,
                 area_m2=500.0, area_basis="plot",
                 title="Prodam pozemek", ad_text=f"Krasny pozemek 1{NB}500 m2 v obci."),
    ])
    _run(monkeypatch, conn, "--write", "--ignore-rebuild", "--sources", "bazos")
    assert conn.listings[0]["area_m2"] == 1500.0
    # bazos yields no typed measures, and the never-blank rule keeps the row's own.
    assert conn.listings[0]["estate_area"] is None


def test_a_two_decimal_area_does_not_rewrite_itself_every_pass(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Every area column is numeric(*,1), so the row reads back rounded. Comparing the
    fresh 86.19 against the stored 86.2 made the heal rewrite live rows for ever."""
    conn = _Conn([
        _listing(id=9, source="ceskereality", category_main="byt", property_id=99,
                 area_m2=86.2, area_basis="usable", usable_area=86.2,
                 params={"plocha užitná": "86,19 m²"},
                 title="Prodej bytu 2+kk 86,19 m²"),
    ])
    _run(monkeypatch, conn, "--write", "--ignore-rebuild")
    assert conn.updates == []
    assert conn.listings[0]["area_m2"] == 86.2


def test_a_second_pass_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _world()
    _run(monkeypatch, conn, "--write", "--ignore-rebuild")
    first = [dict(r) for r in conn.listings]
    conn.updates.clear()
    _run(monkeypatch, conn, "--write", "--ignore-rebuild")
    assert conn.updates == []
    assert [dict(r) for r in conn.listings] == first
