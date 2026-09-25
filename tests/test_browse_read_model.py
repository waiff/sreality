"""Tests for the Browse read-model patch (toolkit.browse_read_model).

Hermetic: a fake conn records executed SQL (and can be told to raise) so the
DELETE+INSERT shape, the dedup/order of ids, the empty no-op, and the
must-never-abort-the-caller swallow behaviour are all exercised without a DB.

Plus a WIRING guardrail (mirroring test_browse_read_path_guardrail.py): every
identity/asset mutation that changes a browse_projection column MUST call
sync_browse_list, so a future refactor can't silently reopen the 2026-07-12
"merge did nothing, then fixed itself" staleness gap.
"""

from __future__ import annotations

import inspect
from typing import Any

import psycopg

from toolkit.browse_read_model import sync_browse_list


class _Ctx:
    def __enter__(self) -> "_Ctx":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


class _Cur:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        if self._conn.raise_on and self._conn.raise_on in s:
            raise psycopg.OperationalError("simulated read-model failure")


class _FakeConn:
    def __init__(self, raise_on: str | None = None) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.raise_on = raise_on

    def transaction(self) -> _Ctx:
        return _Ctx()

    def cursor(self) -> _Cur:
        return _Cur(self)


def test_emits_delete_then_insert_for_the_ids():
    conn = _FakeConn()
    sync_browse_list(conn, [10, 20])
    assert conn.executed[0][0].startswith("DELETE FROM browse_list")
    assert conn.executed[0][1] == ([10, 20],)
    # INSERT re-materializes FROM the shared projection (single column contract).
    assert "INSERT INTO browse_list SELECT * FROM browse_projection" in conn.executed[1][0]
    assert conn.executed[1][1] == ([10, 20],)


def test_dedups_and_preserves_order():
    conn = _FakeConn()
    sync_browse_list(conn, [20, 20, 10, 10])
    assert conn.executed[0][1] == ([20, 10],)


def test_noop_on_empty():
    conn = _FakeConn()
    sync_browse_list(conn, [])
    assert conn.executed == []


def test_swallows_db_error_so_the_caller_write_still_commits():
    # browse_list is a disposable cache; a patch failure must be logged and
    # swallowed, never propagated to abort the merge/link it rides with.
    conn = _FakeConn(raise_on="INSERT INTO browse_list")
    sync_browse_list(conn, [10])  # must NOT raise


# --- Wiring guardrail: every column-changing mutation patches the read model ---


def test_all_identity_and_asset_mutations_call_sync():
    from toolkit import asset_identity, property_identity

    required = [
        property_identity.merge_property_set,
        property_identity.detach_listing,
        asset_identity.link_properties,
        asset_identity.unlink_property,
    ]
    for fn in required:
        src = inspect.getsource(fn)
        assert "sync_browse_list(" in src, (
            f"{fn.__module__}.{fn.__name__} mutates a browse_projection column "
            f"but does not patch browse_list — Browse would go stale for up to a "
            f"rebuild interval (see docs/design/browse-merge-consistency.md)."
        )


def test_the_incremental_maintenance_pass_patches_what_it_recomputed():
    """A15. `browse_projection` reads `properties`, so a post-publication column fill is
    invisible to Browse until `properties` is recomputed AND `browse_list` is
    re-materialized. The dirty-set lane does the first every ~2 min; the second used to
    wait for the */15 wholesale rebuild (measured mean 11.7 min, worst 36.6). The patch
    belongs HERE and nowhere upstream: any earlier caller would re-materialize from a
    `properties` row that has not been recomputed yet."""
    from scripts import recompute_property_stats as rps

    drain = inspect.getsource(rps._drain_dirty)
    assert "sync_browse_list(conn, ids)" in drain
    assert drain.index("_RECOMPUTE_SCOPED_SQL") < drain.index("sync_browse_list"), (
        "the patch must follow the recompute, or it materializes the stale row"
    )
    assert drain.index("sync_browse_list") < drain.index("_DELETE_DIRTY_SQL"), (
        "dequeue last, so a crash between the two replays both (each is idempotent)"
    )
    # The daily full sweep must NOT patch: it recomputes every property, and the
    # wholesale rebuild is already that job's read-model half. One call site, in the
    # dirty-set drain.
    calls = [ln for ln in inspect.getsource(rps).splitlines()
             if "sync_browse_list(" in ln and not ln.lstrip().startswith("#")
             and not ln.startswith("from ")]
    assert calls == ["        sync_browse_list(conn, ids)"], calls
