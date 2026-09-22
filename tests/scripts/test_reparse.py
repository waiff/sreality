"""The ONE re-parse seam: what it may touch, what it may never touch, and idempotence.

The seam writes the hottest table from state we already hold, so every rail is asserted
here rather than trusted: the substrate is declared per portal (and the declaration matches
which portals actually stage a body), the write statement can never mint history or fake a
sighting, a re-derive that yields nothing can never blank a stored value, and a second pass
over the same page reports no change. The idnes case is the substrate proof — its amenity
booleans are DOM icons that serialise to JSON null, so a raw_json seam would read None where
the page reads True.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scraper.db import LISTING_COLUMNS, _PRESERVE_IF_NULL_COLUMNS
from scraper.scraped_listing import _HASH_FIELDS
from scripts import backfill_support as support
from scripts import reparse as mod

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _page(name: str) -> str:
    return (_FIXTURES / "portal_html" / name).read_text(encoding="utf-8")


# --- the declared substrate ---------------------------------------------------


def test_every_portal_declares_a_substrate_and_an_entry_point() -> None:
    import importlib

    from scraper.portal import _DEFAULTS  # the fleet, spelled once

    assert set(mod.SUBSTRATE) == set(_DEFAULTS)
    for source, spec in mod.SUBSTRATE.items():
        assert spec.kind in (mod.PAGE, mod.RAW_JSON)
        entry = getattr(importlib.import_module(spec.module), spec.entry, None)
        assert callable(entry), f"{source}: {spec.module}.{spec.entry} is not callable"
        assert not spec.entry.startswith("_"), f"{source}: the seam calls PUBLIC entry points"


def test_raw_json_is_declared_for_exactly_the_two_portals_that_stage_no_body() -> None:
    """sreality posts its estate JSON and bezrealitky its GraphQL advert; neither writes a
    detail page, so `portal_raw_pages` holds ZERO rows for them (measured 2026-09-21) and
    raw_json is not a preference there but the only substrate. Every HTML portal stages a
    body on every detail fetch, in the same transaction as the listings row."""
    raw = {s for s, spec in mod.SUBSTRATE.items() if spec.kind == mod.RAW_JSON}

    assert raw == {"sreality", "bezrealitky"}


def test_the_category_argument_is_declared_only_where_the_parser_demands_it() -> None:
    """remax and maxima default it to None and read the page, so the seam passes nothing and
    their categories DO re-derive. bazos/ceskereality/idnes declare it without a default."""
    import inspect
    import importlib

    for source, spec in mod.SUBSTRATE.items():
        if spec.kind != mod.PAGE:
            continue
        params = inspect.signature(
            getattr(importlib.import_module(spec.module), spec.entry)).parameters
        required = (
            "category_main" in params
            and params["category_main"].default is inspect.Parameter.empty
        )
        assert spec.takes_category == required, source


# --- what may be healed ------------------------------------------------------


def test_the_two_registries_still_describe_the_live_contract() -> None:
    """Both are DERIVED from their one definition (`_PRESERVE_IF_NULL_COLUMNS`,
    `_HASH_FIELDS`), so a new column extends the deferral gate on its own and there is no
    copy to drift. What still needs pinning is the shape the docstring and the gate argue
    from: exactly two preserve-if-null columns, and `area_basis` as the ONE healable column
    outside the content hash — the only one whose heal churns no snapshot anywhere."""
    assert _PRESERVE_IF_NULL_COLUMNS == {"published_at", "source_url"}
    assert set(mod.HEALABLE) == set(LISTING_COLUMNS) - _PRESERVE_IF_NULL_COLUMNS
    assert set(mod.HEALABLE) - set(_HASH_FIELDS) == {"area_basis"}


def test_fields_is_validated_against_the_registry() -> None:
    import argparse

    assert mod._fields_arg("has_lift, cellar ") == ("has_lift", "cellar")
    with pytest.raises(argparse.ArgumentTypeError):
        mod._fields_arg("source_url")
    with pytest.raises(argparse.ArgumentTypeError):
        mod._fields_arg("last_seen_at")
    with pytest.raises(argparse.ArgumentTypeError):
        mod._fields_arg("  ")


# --- the write path (R9) -----------------------------------------------------


def test_the_write_never_mints_history_and_never_fakes_a_sighting() -> None:
    sql = mod._update_sql(("has_lift", "area_m2"))

    assert "listing_snapshots" not in sql
    assert "last_seen_at" not in sql
    assert "inactive_at" not in sql and "is_active" not in sql
    # one table, one SET clause: a heal of two columns cannot restate a third
    assert sql.count("UPDATE listings") == 1
    assert "SET has_lift = u.has_lift,\n            area_m2 = u.area_m2" in sql


def test_the_whole_module_names_neither_history_nor_the_sighting_clock() -> None:
    body = Path(mod.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#"))
    # the docstring explains both rules by name, so scan the executable half only
    code = code.split('"""', 2)[-1]

    assert "listing_snapshots" not in code
    assert "last_seen_at" not in code


def test_the_write_is_a_compare_and_set_on_every_column_it_touches() -> None:
    """The re-derive happens in Python between the SELECT and the UPDATE while the drain
    keeps writing, and this seam leaves no trace (no snapshot, no `last_seen_at`), so a
    reversion would be invisible. Each chosen column therefore carries the value this pass
    read; a row someone else moved in the window keeps their value."""
    sql = mod._update_sql(("has_lift", "area_m2"))

    assert "AND l.has_lift IS NOT DISTINCT FROM u.was_has_lift" in sql
    assert "AND l.area_m2 IS NOT DISTINCT FROM u.was_area_m2" in sql
    # the read-back array goes in as the COLUMN's own type, so the compare is exact
    assert "%(was_area_m2)s::numeric[]" in sql


def test_every_built_statement_survives_psycopgs_placeholder_tokenizer() -> None:
    """The seam's SQL is built, not a module-level `*_SQL` constant, so neither the offline
    placeholder guard nor the schema-aware PREPARE sweep discovers it — a stray literal `%`
    (a `LIKE 'rebuild\\_%'`, a prose `~2%` in a comment) would raise `incomplete
    placeholder` only on a live dispatch. Run the same checker over what is executed."""
    from tests.test_sql_placeholders import _invalid_placeholder

    fields = ("has_lift", "area_m2", "description", "parking_lots")
    for source in mod.SUBSTRATE:
        for missing in (False, True):
            built = mod._select_sql(source, fields, missing=missing)
            assert _invalid_placeholder(built) is None, f"{source} select: {built}"
    for missing in (False, True):
        assert _invalid_placeholder(mod._count_sql(fields, missing=missing)) is None
    assert _invalid_placeholder(mod._update_sql(fields)) is None
    for guard in (*mod._BATCH_GUARDS, mod._STATEMENT_TIMEOUT_SQL):
        assert _invalid_placeholder(guard) is None


def test_the_write_enqueues_dirty_properties_in_the_same_statement() -> None:
    """Rule 20: the mark must not be able to survive a rolled-back write, so it is one CTE,
    not a second statement — and it is keyed on the row the UPDATE actually touched."""
    sql = mod._update_sql(("condition",))

    assert "INSERT INTO dirty_properties" in sql
    assert "RETURNING l.property_id" in sql
    assert "ON CONFLICT (property_id) DO UPDATE SET marked_at = now()" in sql
    assert sql.index("UPDATE listings") < sql.index("INSERT INTO dirty_properties")


def test_every_batch_arms_its_guards_and_they_need_a_transaction() -> None:
    assert any("statement_timeout" in g for g in mod._BATCH_GUARDS)
    assert any("lock_timeout" in g for g in mod._BATCH_GUARDS)
    for guard in mod._BATCH_GUARDS:
        assert guard.startswith("SET LOCAL ")


def test_the_rails_are_the_shared_ones_not_a_second_copy() -> None:
    assert mod.execute_with_lock_retry is support.execute_with_lock_retry
    assert mod.wait_for_rebuild_gap is support.wait_for_rebuild_gap
    assert mod._STATEMENT_TIMEOUT_SQL is support._STATEMENT_TIMEOUT_SQL


def test_the_missing_narrowing_demands_every_chosen_column_be_null() -> None:
    sql = mod._select_sql("idnes", ("has_lift", "cellar"), missing=True)

    assert "(l.has_lift IS NULL AND l.cellar IS NULL)" in sql
    assert "IS NULL" not in mod._select_sql("idnes", ("has_lift",), missing=False)


# --- never blank -------------------------------------------------------------


def test_a_re_derive_that_yields_none_cannot_overwrite_a_stored_value() -> None:
    """The rule that makes a partial parse failure harmless: a shape drift must never turn
    a stored fact into NULL. It is asserted on a boolean, a number and an enum at once."""
    fields = ("has_lift", "area_m2", "condition")
    stored = {"has_lift": True, "area_m2": 74.0, "condition": "dobry"}
    produced = {c: None for c in LISTING_COLUMNS}

    fresh = mod._merged(produced, stored, fields)

    assert fresh == {"has_lift": True, "area_m2": 74.0, "condition": "dobry"}
    assert mod._moved(stored, fresh, fields) == ()


def test_an_empty_string_counts_as_absent_not_as_a_blank() -> None:
    stored = {"description": "the advert body"}
    produced = {c: None for c in LISTING_COLUMNS} | {"description": "   "}

    assert mod._merged(produced, stored, ("description",)) == {
        "description": "the advert body"}


def test_a_value_the_column_cannot_hold_is_absent_not_an_aborted_batch() -> None:
    """`sane_price_czk` + `sane_listing_numerics` are the ingest boundary's own functions,
    so the seam writes exactly what a live detail write would — a 0 m2 placeholder and a
    2147483647 price become absent, and never-blank then keeps what the row has."""
    stored = {"price_czk": 4_500_000, "area_m2": 68.0}
    produced = {c: None for c in LISTING_COLUMNS} | {"price_czk": 2_147_483_647,
                                                     "area_m2": 0}

    assert mod._merged(produced, stored, ("price_czk", "area_m2")) == {
        "price_czk": 4_500_000, "area_m2": 68.0}


def test_comparison_is_at_column_scale_half_up_so_a_pass_converges() -> None:
    """numeric(*,1) rounds on the way in; compare at the same scale or "86,19 m2" rewrites
    86.2 forever. HALF-UP like Postgres, not round()'s banker's rounding."""
    assert mod._at_column_scale("area_m2", 86.19) == 86.2
    assert mod._at_column_scale("area_m2", 86.25) == 86.3
    assert mod._moved({"area_m2": 86.2}, {"area_m2": 86.2}, ("area_m2",)) == ()


# --- the substrate proof: idnes booleans are icons, not text -----------------


def test_idnes_booleans_re_derive_from_the_stored_page_and_not_from_raw_json() -> None:
    """THE reason the substrate is declared per portal.

    `raw_json['params']` is `{label: _text(dd)}`. An idnes amenity that is a bare check
    icon has no text, so its key is present with a JSON null: True survives only as
    key-presence and False is unrepresentable. Measured over the 3,000 newest active idnes
    byt rows (2026-09-21): 'výtah' present on 1,328, text non-null on 0, has_lift true on
    1,328, false on 0. This fixture carries the same shape on `sklep` and `lodžie`.
    """
    from selectolax.parser import HTMLParser

    from scraper.idnes_parser import _detail_params, _text

    html = _page("idnes_detail.html")
    params = _detail_params(HTMLParser(html))
    stored_raw_json_params = {k: _text(v) for k, v in params.items()}

    # what a raw_json substrate would see: the key is there, the value is not
    assert "sklep" in stored_raw_json_params and "lodžie" in stored_raw_json_params
    assert stored_raw_json_params["sklep"] is None
    assert stored_raw_json_params["lodžie"] is None

    # what the declared substrate — the stored page — yields
    produced = mod._derive(
        "idnes", body=html,
        ref="https://reality.idnes.cz/detail/prodej/dum/x/6ab1/",
        category_main="dum", category_type="prodej")

    assert produced["cellar"] is True
    assert produced["has_balcony"] is True

    # and the never-blank rule means the raw_json reading could only ever keep the stored
    # value, never clear it — the failure mode a raw_json-only seam would have had
    kept = mod._merged({c: None for c in LISTING_COLUMNS},
                       {"cellar": True, "has_balcony": True}, ("cellar", "has_balcony"))
    assert kept == {"cellar": True, "has_balcony": True}


# --- idempotence, offline ----------------------------------------------------


_PAGE_CASES: tuple[tuple[str, str, str], ...] = (
    ("idnes", "idnes_detail.html", "https://reality.idnes.cz/detail/prodej/dum/x/6ab1/"),
    ("realitymix", "realitymix_detail.html",
     "https://reality.centrum.cz/detail/prodej-bytu-3-1/123456789/"),
    ("remax", "remax_detail.html", "https://www.remax-czech.cz/reality/detail/445483/x/"),
    ("mmreality", "mmreality_detail.html",
     "https://www.mmreality.cz/nemovitosti/detail/1234567/"),
    ("bazos", "bazos_detail.html", "https://reality.bazos.cz/inzerat/12345678/x.php"),
)


@pytest.mark.parametrize("source,fixture,url", _PAGE_CASES)
def test_a_second_pass_over_the_same_stored_page_changes_nothing(
    source: str, fixture: str, url: str,
) -> None:
    """Idempotence without a database: pass one writes what the parse produced, pass two
    compares the same parse against it and must report no movement. A re-derive, never
    arithmetic — `floor = floor - 1` would move on every pass."""
    html = _page(fixture)
    fields = tuple(c for c in mod.HEALABLE if c != "area_basis")

    produced = mod._derive(source, body=html, ref=url,
                           category_main="byt", category_type="prodej")
    empty: dict[str, Any] = {c: None for c in fields}
    first = mod._merged(produced, empty, fields)
    assert mod._moved(empty, first, fields), f"{source}: the fixture produced nothing"

    second = mod._merged(produced, first, fields)

    assert second == first
    assert mod._moved(first, second, fields) == ()


def test_the_floor_heal_converges_in_one_pass_and_never_decrements_twice() -> None:
    """W8's heal is `--fields floor` through this seam, and the hazard it exists to avoid
    is arithmetic: the parser deploy and the heal cannot be atomic, so a row the drain
    already rewrote must not be decremented again. The seam re-derives the storey from the
    stored payload, so pass one moves a stale ground=1 row down by one and pass two —
    reading the same payload against the healed column — moves nothing."""
    raw = json.loads((_FIXTURES / "sample_listing.json").read_text(encoding="utf-8"))
    produced = mod._derive("sreality", body=raw, ref=None,
                           category_main=None, category_type=None)

    assert raw["floor_number"] == 1 and produced["floor"] == 0

    stale = {"floor": raw["floor_number"]}          # what the column held pre-W8
    first = mod._merged(produced, stale, ("floor",))
    assert first == {"floor": 0}
    assert mod._moved(stale, first, ("floor",)) == ("floor",)

    second = mod._merged(produced, first, ("floor",))
    assert second == first
    assert mod._moved(first, second, ("floor",)) == ()


def test_the_sreality_raw_json_substrate_round_trips_the_same_way() -> None:
    raw = json.loads((_FIXTURES / "sample_listing.json").read_text(encoding="utf-8"))
    fields = ("area_m2", "floor", "condition", "has_lift", "ownership")

    produced = mod._derive("sreality", body=raw, ref=None,
                           category_main=None, category_type=None)
    empty: dict[str, Any] = {c: None for c in fields}
    first = mod._merged(produced, empty, fields)

    assert first["area_m2"] is not None
    assert mod._merged(produced, first, fields) == first


def test_the_seam_re_derives_the_area_columns_the_deleted_heals_owned() -> None:
    """The one-off area heals called `areas_from_params` / `areas_from_text` directly; the
    seam gets the same numbers through the parser that calls them, so the defect they healed
    is reachable again if it ever regenerates — without a new script."""
    produced = mod._derive(
        "remax", body=_page("remax_detail.html"),
        ref="https://www.remax-czech.cz/reality/detail/445483/x/",
        category_main=None, category_type=None)

    assert any(produced[c] is not None
               for c in ("area_m2", "usable_area", "estate_area", "garden_area"))
    assert produced["area_basis"] is not None


# --- the CLI gates -----------------------------------------------------------


def test_writing_a_hashed_column_refuses_without_the_deferral_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """A hashed column cannot be repaired snapshot-free: the row's next detail fetch
    appends the one genuine snapshot. `--allow-snapshot-deferral` makes that a choice."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://never-used")
    monkeypatch.setattr(
        "sys.argv", ["reparse", "--source", "idnes", "--fields", "has_lift", "--write"])

    assert mod.main() == 2
    err = capsys.readouterr().err
    assert "--allow-snapshot-deferral" in err
    assert "next detail scrape" in err


def test_the_gate_states_srealitys_opposite_consequence_not_the_fleet_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """sreality's drain hashes the RAW payload (`scraper/main.py`), not the parsed fields
    (`scraper.db.write_details`), so a column heal changes no hash and its next detail fetch
    appends NOTHING — ever. Acknowledging "the snapshot is deferred" there would be
    acknowledging a consequence that never arrives; the architecture doc records the same
    asymmetry for the W17 land heal's 44,237 sreality rows."""
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://never-used")
    monkeypatch.setattr(
        "sys.argv",
        ["reparse", "--source", "sreality", "--fields", "condition", "--write"])

    assert mod.main() == 2
    err = capsys.readouterr().err
    assert "NO snapshot is ever appended" in err
    assert "next detail scrape" not in err


def test_an_unhashed_column_needs_no_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """area_basis is out of the hash, so healing it churns nothing and the gate stays shut.
    It stops at the env check instead, which is the next thing main() asserts."""
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setattr(
        "sys.argv", ["reparse", "--source", "idnes", "--fields", "area_basis", "--write"])

    assert mod.main() == 2  # SUPABASE_DB_URL, not the deferral gate


# --- a dry run writes nothing ------------------------------------------------


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self.rowcount = 0

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append(sql)
        self._conn.last = sql

    def fetchone(self) -> tuple[int, int]:
        return (1, 1)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._conn.pages.pop(0) if self._conn.pages else []


class _Conn:
    def __init__(self, pages: list[list[tuple[Any, ...]]]) -> None:
        self.pages = pages
        self.executed: list[str] = []
        self.last = ""

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def __enter__(self) -> "_Conn":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_a_dry_run_reads_the_substrate_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    html = _page("idnes_detail.html")
    # id, property_id, category_main, category_type, ref, body, stored cellar
    page = [(11, 7, "dum", "prodej",
             "https://reality.idnes.cz/detail/prodej/dum/x/6ab1/", html, None)]
    conn = _Conn([page])
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://never-used")
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(mod, "execute_with_lock_retry", _refuse)
    monkeypatch.setattr(mod, "wait_for_rebuild_gap", _refuse)
    monkeypatch.setattr("sys.argv",
                        ["reparse", "--source", "idnes", "--fields", "cellar"])

    with caplog.at_level("INFO"):
        assert mod.main() == 0

    assert not any("UPDATE listings" in s for s in conn.executed)
    assert "cellar           would change=1" in caplog.text
    assert "cellar: None -> True" in caplog.text


def test_a_substrate_that_cannot_be_parsed_is_warned_about_not_counted_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """sreality's oldest rows hold the pre-unwrap payload — no `hash_id`, no `id` — so
    `parse_listing` raises on every one of them (234 of the 1,000 lowest ids carry a usable
    key, measured 2026-09-21). They are skipped per row, which is right, but a clean exit
    that mentions them only inside one INFO line would read as "this portal is done"."""
    page = [(11, 7, None, None, "https://www.sreality.cz/detail/x",
             {"_embedded": {}, "items": [], "locality": {}}, None)]
    conn = _Conn([page])
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://never-used")
    monkeypatch.setattr(mod.db, "connect", lambda *a, **k: conn)
    monkeypatch.setattr("sys.argv",
                        ["reparse", "--source", "sreality", "--fields", "condition"])

    with caplog.at_level("INFO"):
        assert mod.main() == 0

    assert "could not be parsed from their stored substrate" in caplog.text
    assert any(r.levelname == "WARNING" for r in caplog.records)


def _refuse(*args: Any, **kwargs: Any) -> int:
    raise AssertionError("a dry run must neither write nor wait for a rebuild gap")


def test_a_cancelled_page_read_is_replayed_and_a_defect_is_not(monkeypatch: Any) -> None:
    """A statement_timeout on the page SELECT is weather (the map-view rebuild), so the
    same page is read again after a pause; any other error surfaces at once."""
    import psycopg

    from scripts import reparse as r

    monkeypatch.setattr(r.time, "sleep", lambda _s: None)
    calls: list[int] = []

    class _Cur:
        def __init__(self, outcomes: list[Any]) -> None:
            self._outcomes = outcomes

        def __enter__(self) -> "_Cur":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, _sql: str, _params: dict[str, Any]) -> None:
            calls.append(1)
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            self._rows = outcome

        def fetchall(self) -> list[Any]:
            return self._rows

    class _Conn:
        def __init__(self, outcomes: list[Any]) -> None:
            self._outcomes = outcomes

        def cursor(self) -> _Cur:
            return _Cur(self._outcomes)

    cancelled = psycopg.errors.QueryCanceled("canceling statement due to statement timeout")
    conn = _Conn([cancelled, cancelled, [(1,), (2,)]])
    assert r._read_page(conn, "select 1", {"after": 0}, label="t") == [(1,), (2,)]
    assert len(calls) == 3

    calls.clear()
    conn = _Conn([cancelled] * r._PAGE_READ_ATTEMPTS)
    with pytest.raises(psycopg.errors.QueryCanceled):
        r._read_page(conn, "select 1", {"after": 0}, label="t")
    assert len(calls) == r._PAGE_READ_ATTEMPTS

    calls.clear()
    conn = _Conn([psycopg.errors.UndefinedColumn("boom")])
    with pytest.raises(psycopg.errors.UndefinedColumn):
        r._read_page(conn, "select 1", {"after": 0}, label="t")
    assert len(calls) == 1
