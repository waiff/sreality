"""Tests for scripts.recompute_property_stats pure helpers.

Hermetic: the id-batching arithmetic, the fake-conn execution order, and the
static validity of every SQL constant's `%`-placeholders are exercised here; the
SQL's runtime semantics + DB I/O are verified out-of-band via the Supabase MCP
after the migrations apply.
"""

from __future__ import annotations

from typing import Any

import pytest

from scripts.recompute_property_stats import (
    _attach_stragglers,
    _batch_ranges,
    _drain_dirty,
    _publish_sweep,
)


def test_empty_when_no_properties():
    assert list(_batch_ranges(0, 2000)) == []


def test_invalid_batch_size_yields_nothing():
    assert list(_batch_ranges(100, 0)) == []


def test_half_open_ranges_cover_exact_multiple():
    assert list(_batch_ranges(4, 2)) == [(1, 3), (3, 5)]


def test_last_range_overshoots_to_cover_remainder():
    assert list(_batch_ranges(5, 2)) == [(1, 3), (3, 5), (5, 7)]


def test_every_id_lands_in_exactly_one_range():
    max_id, batch = 71_556, 2000
    seen = 0
    for lo, hi in _batch_ranges(max_id, batch):
        # half-open [lo, hi); count the ids in [lo, min(hi-1, max_id)]
        seen += min(hi - 1, max_id) - lo + 1
    assert seen == max_id


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
    def __init__(self, script: list[tuple[Any, list[tuple[Any, ...]]]] | None = None) -> None:
        self.script = script or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)


def _sqls(conn: _FakeConn) -> list[str]:
    return [e[0] for e in conn.executed]


def _find(conn: _FakeConn, needle: str) -> tuple[str, Any] | None:
    return next((e for e in conn.executed if needle in e[0]), None)


def test_attach_stragglers_singletons_only_no_spatial_link():
    """Stragglers become singletons; the old geo spatial-link step is gone.

    Matching is the out-of-band street+disposition dedup engine's job, so
    attach must NOT run any ST_DWithin probe or enqueue dirty_properties — it
    only inserts a singleton per unlinked listing and links it.
    """
    conn = _FakeConn()
    _attach_stragglers(conn)
    order = _sqls(conn)
    insert = next(i for i, s in enumerate(order) if "INSERT INTO properties" in s)
    link = next(i for i, s in enumerate(order) if "p.repr_listing_id = l.sreality_id" in s)
    assert insert < link
    assert not any("ST_DWithin" in s for s in order)
    assert not any("INSERT INTO dirty_properties" in s for s in order)


def test_attach_stragglers_full_runs_native_id_backfill():
    conn = _FakeConn()
    _attach_stragglers(conn)
    assert any("source_id_native = sreality_id::text" in s for s in _sqls(conn))


def test_attach_stragglers_incremental_skips_native_id_backfill():
    """The */5 incremental pass must not scan the whole listings table for the
    one-time native-id backfill; the daily full sweep handles it."""
    conn = _FakeConn()
    _attach_stragglers(conn, skip_native_backfill=True)
    order = _sqls(conn)
    assert not any("source_id_native = sreality_id::text" in s for s in order)
    # still inserts singletons even when the backfill is skipped
    assert any("INSERT INTO properties" in s for s in order)


class _DrainCur:
    def __init__(self, conn: "_DrainConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_DrainCur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if s.startswith("DELETE FROM dirty_properties"):
            self._conn.deleted.append((params["ids"], params["cutoff"]))
            self._rows = []
        elif "SELECT property_id, marked_at FROM dirty_properties" in s:
            self._rows = self._conn.batches.pop(0) if self._conn.batches else []
        elif "WITH batch AS" in s:  # scoped recompute
            self._conn.recomputed.append(params["ids"])
            self._rows = []
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _DrainConn:
    def __init__(self, batches: list[list[tuple[Any, ...]]]) -> None:
        self.batches = list(batches)
        self.executed: list[tuple[str, Any]] = []
        self.recomputed: list[list[int]] = []
        self.deleted: list[tuple[list[int], Any]] = []

    def cursor(self) -> _DrainCur:
        return _DrainCur(self)


def test_drain_dirty_recomputes_each_batch_then_terminates():
    conn = _DrainConn([[(7, "t1"), (8, "t1")], [(9, "t2")], []])
    total = _drain_dirty(conn, batch_size=2, cutoff="CUTOFF")
    assert total == 3
    assert conn.recomputed == [[7, 8], [9]]
    # deletes are scoped to the claimed ids and the run cutoff
    assert conn.deleted == [([7, 8], "CUTOFF"), ([9], "CUTOFF")]


def test_drain_dirty_empty_queue_is_noop():
    conn = _DrainConn([[]])
    assert _drain_dirty(conn, 100, "C") == 0
    assert conn.recomputed == []
    assert conn.deleted == []


def test_publish_sweep_only_touches_unpublished_ineligible():
    """The ineligible publish sweep (migration 273) publishes ONLY unpublished, active
    properties whose repr listing is eligible for NONE of the three dedup passes — an
    eligible-but-unchecked property stays NULL (the engine stamps that one), and an
    already-published row is never touched. Row-level semantics run in the DB; here we
    pin the SQL shape that guarantees them + the returned rowcount."""
    from toolkit.publication import (
        BYT_GEO_ELIGIBLE_PREDICATE,
        GEO_ELIGIBLE_PREDICATE,
        STREET_ELIGIBLE_PREDICATE,
    )

    conn = _FakeConn([
        (lambda s: "publish_reason = 'ineligible'" in s, [(1,), (2,)]),  # 2 rows published
    ])
    assert _publish_sweep(conn) == 2

    sweep = _find(conn, "publish_reason = 'ineligible'")
    assert sweep is not None
    sql = " ".join(sweep[0].split())
    assert "p.published_at IS NULL" in sql          # never re-publishes a stamped row
    assert "p.status = 'active'" in sql
    # Repr join is on the SURROGATE, not the legacy sreality_id handle — a
    # non-sreality repr must still be found (pre-Gate-2 hardening, #873-style).
    assert "l.id = p.repr_listing_ref_id" in sql
    assert "l.sreality_id = p.repr_listing_id" not in sql
    # ALL THREE eligibility predicates, each wrapped IS NOT TRUE (NULL-safe
    # ineligibility) so an eligible repr listing keeps the property NULL for the
    # engine to stamp.
    for pred in (STREET_ELIGIBLE_PREDICATE, GEO_ELIGIBLE_PREDICATE,
                 BYT_GEO_ELIGIBLE_PREDICATE):
        assert f"({' '.join(pred.split())}) IS NOT TRUE" in sql


def test_enqueue_imageless_routes_to_dedup_dirty_lane():
    """The zero-image EVALUATION sweep routes unpublished, dedup-ELIGIBLE, imageless
    properties into dedup_dirty_properties — an evaluation trigger (the engine still
    decides + stamps them), never a publish-timeout. Pins the SQL shape: age gate,
    the shared street-OR-geo predicate, the zero-stored-images anti-join, and the
    exclude-already-queued guard (no marked_at bump — newest-first priority intact)."""
    from scripts.recompute_property_stats import (
        IMAGELESS_EVAL_MINUTES,
        _enqueue_imageless_for_dedup,
    )
    from toolkit.publication import eligible_predicate

    conn = _FakeConn([
        (lambda s: "INSERT INTO dedup_dirty_properties" in s, [(1,)]),
    ])
    assert _enqueue_imageless_for_dedup(conn) == 1

    entry = _find(conn, "INSERT INTO dedup_dirty_properties")
    assert entry is not None
    sql = " ".join(entry[0].split())
    assert "p.published_at IS NULL" in sql
    assert "p.status = 'active'" in sql
    assert f"interval '{IMAGELESS_EVAL_MINUTES} minutes'" in sql
    # dedup-eligible via the SHARED street-OR-geo predicate, property-grain EXISTS.
    assert " ".join(eligible_predicate("le").split()) in sql
    assert "le.property_id = p.id" in sql
    # zero STORED images across ALL the property's listings.
    assert "i.storage_path IS NOT NULL" in sql
    # already-queued rows are EXCLUDED, not bumped (bumping resets newest-first order).
    assert "FROM dedup_dirty_properties d WHERE d.property_id = p.id" in sql
    assert "DO NOTHING" in sql and "DO UPDATE" not in sql
    # never writes published_at itself — publishing stays the engine's stamp.
    assert "published_at = " not in sql


class _MainConn(_FakeConn):
    """Context-manager conn for driving main(): serves the cutoff SELECT and
    the maintenance lease CAS (acquired), records the rest."""

    def __init__(self) -> None:
        super().__init__([
            (lambda s: s == "SELECT now()", [("CUTOFF",)]),
            (lambda s: "property_maintenance_lease" in s and "RETURNING" in s, [(1,)]),
        ])

    def __enter__(self) -> "_MainConn":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


def _run_main(monkeypatch: Any, argv: list[str]) -> list[str]:
    import sys
    import types

    import scripts.recompute_property_stats as rps

    calls: list[str] = []
    monkeypatch.setenv("SUPABASE_DB_URL", "postgres://test")
    monkeypatch.setattr(sys, "argv", ["recompute_property_stats", *argv])
    monkeypatch.setitem(
        sys.modules, "psycopg",
        types.SimpleNamespace(connect=lambda *a, **k: _MainConn()))
    monkeypatch.setattr(
        rps, "_attach_stragglers", lambda c, **k: calls.append("attach") or 0)
    monkeypatch.setattr(
        rps, "_drain_dirty", lambda c, bs, cutoff: calls.append("drain") or 0)
    monkeypatch.setattr(rps, "_publish_sweep", lambda c: calls.append("publish") or 0)
    monkeypatch.setattr(
        rps, "_enqueue_imageless_for_dedup", lambda c: calls.append("imageless") or 0)
    monkeypatch.setattr(rps, "_reconcile_childless", lambda c: 0)
    monkeypatch.setattr(rps, "_max_property_id", lambda c: 0)
    assert rps.main() == 0
    return calls


def test_incremental_runs_imageless_sweep_after_publish(monkeypatch: Any) -> None:
    """--incremental (the */5 cron) runs the imageless evaluation sweep right after the
    ineligible publish sweep — same placement pattern, O(new unpublished) each pass."""
    assert _run_main(monkeypatch, ["--incremental"]) == [
        "attach", "drain", "publish", "imageless"]


def test_full_mode_skips_publish_and_imageless_sweeps(monkeypatch: Any) -> None:
    """The daily full sweep matches _publish_sweep's placement: neither publish nor the
    imageless enqueue runs there (both are the incremental pass's job)."""
    calls = _run_main(monkeypatch, [])
    assert calls == ["attach"]


def test_publication_predicates_parity_with_engine():
    """toolkit.publication mirrors the engine's eligibility VERBATIM (single source), so
    the ineligible sweep can never publish a property the engine WOULD dedup-check. A
    drift in any predicate fails here. Covers EVERY engine SQL constant that embeds
    an eligibility predicate — including the dirty-drain claim scopers, which once
    hand-inlined the street predicate (an untested drift risk)."""
    import scripts.dedup_engine as eng

    from toolkit.publication import (
        BYT_GEO_ELIGIBLE_PREDICATE,
        GEO_ELIGIBLE_PREDICATE,
        STREET_ELIGIBLE_PREDICATE,
    )

    assert STREET_ELIGIBLE_PREDICATE == eng._ELIGIBILITY
    assert STREET_ELIGIBLE_PREDICATE in eng._ELIGIBLE_SQL
    assert STREET_ELIGIBLE_PREDICATE in eng._CLAIMED_STREET_GROUPS_SQL
    assert STREET_ELIGIBLE_PREDICATE in eng._CLAIMED_FAMILY_ELIGIBILITY_SQL
    assert GEO_ELIGIBLE_PREDICATE in eng._GEO_ELIGIBLE_SQL
    assert GEO_ELIGIBLE_PREDICATE in eng._CLAIMED_GEO_CELLS_SQL
    assert GEO_ELIGIBLE_PREDICATE in eng._CLAIMED_FAMILY_ELIGIBILITY_SQL
    assert BYT_GEO_ELIGIBLE_PREDICATE in eng._BYT_GEO_ELIGIBLE_SQL
    assert BYT_GEO_ELIGIBLE_PREDICATE in eng._CLAIMED_BYT_GEO_CELLS_SQL
    assert BYT_GEO_ELIGIBLE_PREDICATE in eng._CLAIMED_FAMILY_ELIGIBILITY_SQL
    # Cross-pass visibility: the cell surfaces must NOT re-grow the AND-NOT-street
    # exclusion (street-eligible cell-family rows participate in the cell passes; only
    # the both-street-eligible PAIR is skipped, in resolve_pair).
    for sql in (eng._GEO_ELIGIBLE_SQL, eng._BYT_GEO_ELIGIBLE_SQL,
                eng._CLAIMED_GEO_CELLS_SQL, eng._CLAIMED_BYT_GEO_CELLS_SQL,
                eng._CLAIMED_FAMILY_ELIGIBILITY_SQL):
        assert f"NOT ({eng._ELIGIBILITY})" not in sql


def _migration_cell_key_lists(filename: str) -> list[tuple[str, ...]]:
    """Every NOT IN category list inside a migration's listing_geo_cell_key() body."""
    import re
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "migrations" / filename).read_text()
    start = text.index("CREATE OR REPLACE FUNCTION public.listing_geo_cell_key")
    body = text[start:text.index("$$;", start)]
    return [tuple(re.findall(r"'([^']*)'", found))
            for found in re.findall(r"NOT IN \(([^)]*)\)", body)]


def test_geo_families_pin_migration_276_sql_twin():
    """publication.GEO_FAMILIES is the single PYTHON source of the geo category list;
    HISTORICAL migration 276's listing_geo_cell_key() carried the four-family SQL twin
    (SQL can't import Python). The 276 file is frozen (rule #1) and must keep matching
    GEO_FAMILIES exactly; the LIVE function is migration 296's (pinned to CELL_FAMILIES
    below). The rendered geo predicate IN-list must still be built from GEO_FAMILIES."""
    from toolkit.publication import GEO_ELIGIBLE_PREDICATE, GEO_FAMILIES

    rendered = "(" + ", ".join(f"'{f}'" for f in GEO_FAMILIES) + ")"
    assert f"category_main IN {rendered}" in GEO_ELIGIBLE_PREDICATE

    lists = _migration_cell_key_lists("276_listings_geo_cell_key.sql")
    assert lists, "migration 276 function body lost its category list"
    for found in lists:
        assert found == GEO_FAMILIES


def test_cell_families_pin_migration_296_sql_twin():
    """publication.CELL_FAMILIES (= GEO_FAMILIES + byt) is the single PYTHON source of
    the CELL-STAMPED category list; migration 296's redefined listing_geo_cell_key()
    — the LIVE function — carries the SQL twin. This pins the two, exactly like the
    276/GEO_FAMILIES pin: the function body's NOT IN list must be EXACTLY
    CELL_FAMILIES, and byt must stay OUT of the dum|komercni collapse (its own
    bucket)."""
    from pathlib import Path

    from toolkit.publication import BYT_GEO_ELIGIBLE_PREDICATE, CELL_FAMILIES, GEO_FAMILIES

    assert CELL_FAMILIES == GEO_FAMILIES + ("byt",)
    assert "l.category_main = 'byt'" in BYT_GEO_ELIGIBLE_PREDICATE
    assert "l.disposition IS NOT NULL" in BYT_GEO_ELIGIBLE_PREDICATE

    lists = _migration_cell_key_lists("296_byt_geo_cell_key.sql")
    assert lists, "migration 296 function body lost its category list"
    for found in lists:
        assert found == CELL_FAMILIES

    # byt buckets to ITSELF: the collapse branch must still name only dum+komercni.
    text = (Path(__file__).resolve().parents[1]
            / "migrations" / "296_byt_geo_cell_key.sql").read_text()
    start = text.index("CREATE OR REPLACE FUNCTION public.listing_geo_cell_key")
    body = text[start:text.index("$$;", start)]
    assert "IN ('dum', 'komercni')" in body
    assert "'dum|komercni'" in body


def test_every_resolved_sql_constant_has_valid_placeholders():
    """All `*_SQL` attributes — including the `.replace()`-derived executors —
    must pass psycopg's placeholder parser.

    The fakes above record SQL without parsing it (which is why a prose `~2%` in
    `_RECOMPUTE_BATCH_SQL` once shipped green and broke property maintenance +
    every merge). This module is uniquely exposed: `_RECOMPUTE_ONE_SQL` and
    `_RECOMPUTE_SCOPED_SQL` are derived from `_RECOMPUTE_BATCH_SQL` at import
    time, so they can't be statically inspected — only validated after they
    resolve. The repo-wide AST guard (tests/test_sql_placeholders.py) covers the
    base constants; this covers the derived family that actually executes.
    """
    import scripts.recompute_property_stats as rps

    split = pytest.importorskip("psycopg._queries")._split_query
    names = [n for n in dir(rps) if n.endswith("_SQL") and isinstance(getattr(rps, n), str)]
    assert {"_RECOMPUTE_BATCH_SQL", "_RECOMPUTE_ONE_SQL", "_RECOMPUTE_SCOPED_SQL"} <= set(names)
    for name in names:
        split(getattr(rps, name).encode())  # raises ProgrammingError on a bad `%`


# --- run_incremental_pass (the shared GH-cron / worker-lane implementation) ----


def _lock_script(acquired: bool):
    """FakeConn script: answer the lease CAS (RETURNING a row iff acquired),
    the cutoff now(), and the dirty claim (empty queue) so a pass runs
    end-to-end without a database."""
    return [
        (lambda s: "property_maintenance_lease" in s and "RETURNING" in s,
         [(1,)] if acquired else []),
        (lambda s: s == "SELECT now()", [("2026-07-08T00:00:00+00:00",)]),
        (lambda s: "FROM dirty_properties" in s and "SELECT" in s, []),
    ]


def test_run_incremental_pass_runs_all_phases_and_unlocks():
    from scripts.recompute_property_stats import run_incremental_pass

    conn = _FakeConn(script=_lock_script(acquired=True))
    stats = run_incremental_pass(conn, batch_size=500)
    assert stats["skipped"] is False
    sqls = _sqls(conn)
    # every phase of the incremental pass ran...
    assert _find(conn, "INSERT INTO properties")  # straggler attach
    assert not any("source_id_native = sreality_id" in s for s in sqls)  # skip legacy backfill
    assert _find(conn, "published_at")  # publish sweep
    assert _find(conn, "dedup_dirty_properties")  # imageless enqueue
    # ...and the lease was released even on the happy path.
    assert _find(conn, "SET holder = NULL")


def test_run_incremental_pass_skips_when_lease_held():
    from scripts.recompute_property_stats import run_incremental_pass

    conn = _FakeConn(script=_lock_script(acquired=False))
    stats = run_incremental_pass(conn, batch_size=500)
    assert stats == {
        "skipped": True, "attached": 0, "recomputed": 0,
        "published": 0, "imageless": 0,
    }
    # NOTHING ran: no attach, no recompute, no publish — and no release either
    # (we never held the lease; clearing it would release someone else's).
    sqls = _sqls(conn)
    assert not any("INSERT INTO properties" in s for s in sqls)
    assert not any("SET holder = NULL" in s for s in sqls)


def test_run_incremental_pass_unlocks_on_failure():
    from scripts.recompute_property_stats import run_incremental_pass

    class _Boom(_FakeConn):
        def cursor(self):
            cur = super().cursor()
            orig = cur.execute

            def execute(sql, params=None):
                if "INSERT INTO properties" in sql:
                    raise RuntimeError("boom")
                return orig(sql, params)

            cur.execute = execute  # type: ignore[method-assign]
            return cur

    conn = _Boom(script=_lock_script(acquired=True))
    with pytest.raises(RuntimeError):
        run_incremental_pass(conn, batch_size=500)
    assert _find(conn, "SET holder = NULL")
