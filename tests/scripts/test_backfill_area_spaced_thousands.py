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
        self._one: tuple | None = None
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
        elif sql.strip().startswith("SELECT count(*)"):
            n = sum(1 for r in self._conn.listings
                    if r["source"] == params["source"])
            self._one = (n, n, n)
        elif sql.strip().startswith("SELECT id"):
            rows = sorted((r for r in self._conn.listings
                           if r["id"] > params["after"]
                           and r["source"] == params["source"]),
                          key=lambda r: r["id"])
            text_lane = "description AS ad_text" in sql
            object_lane = "jsonb_build_object(" in sql
            self._rows = [
                tuple(None if (c == "ad_text" and not text_lane)
                      else None if (c == "params" and text_lane)
                      else None if (c == "title" and object_lane)
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

    def fetchone(self) -> tuple | None:
        return self._one


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
    spec = mod._select_sql("ceskereality")
    assert "raw_json->'params'" in spec
    assert "raw_json->>'title'" in spec
    # bazos has no spec table: the ad body is its substrate, on its own statement so the
    # other portals never detoast `description` for nothing.
    assert "description AS ad_text" in mod._select_sql("bazos")
    assert "description" not in spec
    assert mod.TEXT_SOURCES == frozenset({"bazos"})
    # mmreality's substrate is the estate object itself (`raw = dict(obj)`), so its
    # measures are TOP-LEVEL raw_json keys — projected narrowly, never as the whole blob.
    obj = mod._select_sql("mmreality")
    assert "raw_json->'parcelArea'" in obj and "raw_json->'usableArea'" in obj
    assert "raw_json->'params'" not in obj
    assert mod.OBJECT_SOURCES == frozenset({"mmreality"})


def test_the_portals_own_key_precedence_is_called_never_recopied() -> None:
    """A second copy of a key order is the same defect as a second copy of the number
    grammar, one level up (rule 21). Each parser exposes the function its own
    `parse_detail` calls, and the heal calls exactly that."""
    from scraper.bazos_parser import areas_from_text
    from scraper.ceskereality_parser import areas_from_params as ck
    from scraper.idnes_parser import areas_from_params as idn
    from scraper.maxima_parser import areas_from_params as mx
    from scraper.mmreality_parser import areas_from_params as mm
    from scraper.realitymix_parser import areas_from_params as rmix
    from scraper.remax_parser import areas_from_params as rmax

    assert all(callable(f) for f in (ck, rmix, rmax, mx, idn, mm, areas_from_text))
    # CODE only — the module docstring quotes a key or two as illustration.
    code = _module_code()
    for key in ("plocha pozemku", "užitná plocha", "plocha parcely",
                "celkova plocha", "plocha zahrady", "podlahová plocha",
                "parcelArea", "usableArea", "gardenArea"):
        assert key not in code, f"{key!r} is the parser's to spell, not the heal's"


# --- the selection ------------------------------------------------------------


def test_the_suspect_predicate_is_spelled_once_and_used_everywhere() -> None:
    for source in ("ceskereality", "bazos", "idnes"):
        assert mod._SUSPECT in mod._count_sql(source)
        assert mod._SUSPECT in mod._select_sql(source)
    # a truncation leaves the last three digits ...
    for col in mod._AREA_COLS:
        assert f"{col} < 1000" in mod._SUSPECT
    # ... unless they were "000", which the write boundary NULLed: the all-NULL arm.
    assert "area_m2 IS NULL AND usable_area IS NULL" in " ".join(mod._SUSPECT.split())


def test_mmreality_is_walked_whole_because_it_carries_no_fingerprint() -> None:
    """Its numbers are typed JSON and never met a regex: the defect is a wrong KEY, so the
    `< 1000` arm asks the wrong question AND misses the population — a land row carrying
    the sum as its headline and NULL in every other area column satisfies neither arm
    (3,443 of 14,417 rows). A 14k corpus needs no fingerprint; `_changed` decides."""
    assert mod._suspect_sql("mmreality") == mod._SUSPECT_ALL == "true"
    assert mod._SUSPECT not in mod._select_sql("mmreality")
    assert mod._SUSPECT not in mod._count_sql("mmreality")
    # and the count and the page read agree about which rows the run owns.
    assert mod._suspect_sql("mmreality") in mod._count_sql("mmreality")


def test_no_predicate_ever_touches_a_wide_column() -> None:
    """`raw_json` is PROJECTED per page, which is a fine read; a predicate over it would
    detoast every candidate row, the cost that killed `backfill_mmreality_areas`'s first
    live dispatch on a 120 s statement_timeout."""
    assert "raw_json" not in mod._SUSPECT and "description" not in mod._SUSPECT
    for source in mod.WIRED_SOURCES:
        where = mod._select_sql(source).split("WHERE", 1)[1]
        assert "raw_json" not in where and "description" not in where
        assert "raw_json" not in mod._count_sql(source)


def test_the_page_read_is_one_source_at_a_time() -> None:
    """The paging SELECT measured 40.5 s against the cluster's 120 s default. A page that
    filtered `source = ANY(...)` stepped over every other portal's rows to find its own."""
    for source in mod.WIRED_SOURCES:
        flat = " ".join(mod._select_sql(source).split())
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


def test_the_healed_population_is_every_portal_with_a_derivation_wired() -> None:
    """W19's five carried the naive grammar; W21's two carried a wrong KEY. One job either
    way, because the fix lands inside the function `parse_detail` and the heal share.

    WIRED and WALKED-BY-DEFAULT are deliberately different sets. idnes is wired — it has
    the shared shape and `--sources idnes` runs it — but a bare run skips it: its W21
    change is forward-only (this job never blanks a stored value, and one row corpus-wide
    has anything to retract), so walking its ~206k rows by default would spend the whole
    budget changing ~nothing.
    """
    assert set(mod.WIRED_SOURCES) == {
        "ceskereality", "realitymix", "remax", "maxima", "bazos", "idnes", "mmreality"}
    assert set(mod.DEFAULT_SOURCES) == set(mod.WIRED_SOURCES) - {"idnes"}
    assert mod.EXTRA_SOURCES == ("idnes",)
    # mmreality leads: the only portal whose stored rows are wrong TODAY, and the
    # smallest corpus, so a run that spends `--max-seconds` still finishes it.
    assert mod.DEFAULT_SOURCES[0] == "mmreality"
    # The tuple doubles as the allowlist `--sources` is validated against, so an unwired
    # portal exits 2 rather than raising mid-page with rows already written.
    for source in mod.WIRED_SOURCES:
        assert mod._areas_for(source, params={}, title=None, ad_text=None,
                              category_main=None) is None


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
        # remax: a parcel `area_m2` (numeric(7,1)) cannot store, under remax's OWN parcel
        # label — "Plocha parcely", the only one its pages carry (W20).
        _listing(id=2, source="remax", property_id=22,
                 area_m2=800.0, area_basis="plot", estate_area=800.0,
                 params={"plocha parcely": f"16{NB}809{NB}800 m²"},
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


def test_mmreality_re_keys_the_plot_off_the_parcel_cell(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The W21 heal for the 1,178 active houses whose `estate_area` is the page's
    `parcelArea + usableArea` sum, and the 1,515 that still carry that sum as their
    HEADLINE (the pre-W1 shape, never re-derived). Both re-derive off the SAME stored
    object the live parser reads, through the same function — the heal projects only the
    keys `scraper.mmreality_parser.AREA_OBJECT_KEYS` names, so `totalArea` is not even
    fetched."""
    conn = _Conn([
        # the sum stored as the plot: 450 + 200 = 650.
        _listing(id=31, source="mmreality", category_main="dum", property_id=311,
                 area_m2=200.0, area_basis="usable", usable_area=200.0,
                 estate_area=650.0, garden_area=300.0,
                 params={"usableArea": 200, "parcelArea": 450, "gardenArea": 300}),
        # the pre-W1 shape: the sum as the headline, no plot at all.
        _listing(id=32, source="mmreality", category_main="dum", property_id=322,
                 area_m2=650.0, area_basis="usable", usable_area=200.0,
                 params={"usableArea": 200, "parcelArea": 450, "gardenArea": 300}),
        # land: the parcel IS the headline (rule 23), and 5,000 > 1000 — the row the
        # W19 fingerprint would never have selected.
        _listing(id=33, source="mmreality", category_main="pozemek", property_id=333,
                 area_m2=5000.0, area_basis="plot",
                 params={"parcelArea": 5000}),
    ])
    _run(monkeypatch, conn, "--write", "--ignore-rebuild", "--sources", "mmreality")

    house, legacy, land = conn.listings
    assert (house["area_m2"], house["area_basis"]) == (200.0, "usable")
    assert house["estate_area"] == 450.0
    assert (legacy["area_m2"], legacy["area_basis"]) == (200.0, "usable")
    assert legacy["estate_area"] == 450.0
    assert (land["area_m2"], land["area_basis"]) == (5000.0, "plot")
    assert land["estate_area"] == 5000.0
    assert conn.dirty == {311, 322, 333}


def test_the_mmreality_key_export_is_sufficient_on_its_own() -> None:
    """The heal projects EXACTLY `AREA_OBJECT_KEYS` out of raw_json, so if the parser ever
    reads a key the export omits, the heal silently re-derives from a narrower page than
    the live parse does. An object carrying only the exported keys must still produce
    every area column."""
    from scraper.mmreality_parser import AREA_OBJECT_KEYS, areas_from_params

    obj = dict.fromkeys(AREA_OBJECT_KEYS, 100)
    areas = areas_from_params(obj, category_main="dum")
    assert areas.area_m2 and areas.usable_area
    assert areas.estate_area and areas.garden_area


def test_idnes_heals_its_areas_but_never_blanks_a_stale_usable_area(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """idnes joins the heal, and the never-blank rule bounds what that can fix.

    W21 narrowed idnes's `usable_area` to the "užitná plocha" cell alone (it was
    `užitná or podlahová or plocha`, so a page stating only a floor area wrote that
    number into the column every consumer reads as the užitná measure). The heal cannot
    UNDO the stale write: a column the re-derive produces no value for is carried over
    from the row, because writing NULL over a stored area is indistinguishable from a
    shape drift. The contract is fixed forward; the residue is 1 row corpus-wide
    (measured 2026-09-17: idnes + ceskereality rows carrying a usable_area their own
    headline basis says came from the fallback).

    What the heal DOES fix for idnes is every area the grammar or the parcel slot can
    still move — here a title-only land row whose parcel cell the old private clamps and
    the pre-W17 mapping left unread.
    """
    conn = _Conn([
        _listing(id=41, source="idnes", category_main="byt", property_id=411,
                 area_m2=75.0, area_basis="floor", usable_area=75.0,
                 params={"podlahová plocha": "75 m²"}, title="Prodej bytu 3+1 75 m²"),
        _listing(id=42, source="idnes", category_main="pozemek", property_id=422,
                 area_m2=None, area_basis=None,
                 params={"plocha pozemku": f"1{NB}074 m²"}, title="Prodej pozemku"),
    ])
    _run(monkeypatch, conn, "--write", "--ignore-rebuild", "--sources", "idnes")

    floor_only, land = conn.listings
    assert (floor_only["area_m2"], floor_only["area_basis"]) == (75.0, "floor")
    assert floor_only["usable_area"] == 75.0   # never blanked — see the docstring
    assert (land["area_m2"], land["area_basis"]) == (1074.0, "plot")
    assert land["estate_area"] == 1074.0


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
