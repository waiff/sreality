"""The coverage gate: un-parking a portal on evidence rather than on an opinion.

`supports_complete_walk` gates delisting (rule #3). It was parked on ceskereality
(migration 449) and idnes (453) for the same reason — the flag was a standing
claim someone typed once, and the walks stopped matching it. Un-parking by hand
would recreate exactly that, so the gate re-asks the question on a schedule and
writes down the answer every time.

These tests pin the properties that decide whether that is safe to leave running:
what counts as covered, what counts as stable, and — most importantly — that
every way of being unsure keeps the flag DOWN.
"""

from __future__ import annotations

from typing import Any

from scripts import coverage_gate as cg


class _Cur:
    def __init__(self, conn: "_Conn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        flat = " ".join(sql.split())
        self._conn.executed.append((flat, params))
        if "from portal_index_slices" in flat:
            self._rows = self._conn.coverage
        elif "from listings" in flat:
            self._rows = [(self._conn.candidates,)]
        elif "from portal_coverage_gate" in flat:
            self._rows = self._conn.history
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(
        self,
        coverage: list[tuple[Any, ...]],
        candidates: int = 1000,
        history: list[tuple[Any, ...]] | None = None,
    ) -> None:
        self.coverage = coverage
        self.candidates = candidates
        self.history = history or []
        self.executed: list[tuple[str, Any]] = []

    def cursor(self) -> _Cur:
        return _Cur(self)

    @property
    def unparked(self) -> bool:
        return any("supports_complete_walk = true" in s for s, _ in self.executed)


def _full(n: int = 2) -> list[tuple[Any, ...]]:
    """n categories, each with all 15 slices fresh and exhausted."""
    return [("byt", "prodej", 15, 15), ("dum", "prodej", 15, 15)][:n]


def _covered_history(n: int, candidates: int = 1000) -> list[tuple[Any, ...]]:
    return [(True, candidates)] * n


def test_a_full_sweep_three_times_running_opens_the_gate() -> None:
    conn = _Conn(_full(), candidates=1000, history=_covered_history(2))
    row = cg.evaluate(conn, "idnes", 2, dry_run=False)
    assert row["verdict"] == "unparked"
    assert row["consecutive"] == 3
    assert conn.unparked is True


def test_one_unfinished_slice_keeps_the_flag_down() -> None:
    """Fourteen of fifteen is not 93% coverage for delisting purposes — the hole
    is exactly what mark_inactive reads as 'these listings are gone'."""
    conn = _Conn([("byt", "prodej", 15, 14), ("dum", "prodej", 15, 15)],
                 history=_covered_history(2))
    row = cg.evaluate(conn, "idnes", 2, dry_run=False)
    assert row["covered"] is False
    assert row["verdict"] == "hold"
    assert conn.unparked is False


def test_a_category_that_was_never_walked_keeps_the_flag_down() -> None:
    """The subtle one. A never-walked category has NO ledger rows, so counting
    only what the ledger holds would let a portal pass by walking a subset
    perfectly — which is precisely idnes's failure: 2 of 10 categories walked,
    both to completion. The declared count is the denominator, not the observed
    one."""
    conn = _Conn(_full(2), history=_covered_history(2))
    row = cg.evaluate(conn, "idnes", 10, dry_run=False)   # portal declares 10
    assert row["covered"] is False
    assert "2/10" in row["note"]
    assert conn.unparked is False


def test_two_good_runs_are_not_enough() -> None:
    """One is luck, two is a coincidence."""
    conn = _Conn(_full(), history=_covered_history(1))
    row = cg.evaluate(conn, "idnes", 2, dry_run=False)
    assert row["consecutive"] == 2
    assert row["verdict"] == "hold"
    assert conn.unparked is False


def test_the_streak_resets_on_a_miss() -> None:
    conn = _Conn(_full(), history=[(True, 1000), (False, None), (True, 1000)])
    row = cg.evaluate(conn, "idnes", 2, dry_run=False)
    assert row["consecutive"] == 2   # this one + the one before the miss
    assert conn.unparked is False


def test_a_swinging_candidate_count_keeps_the_flag_down() -> None:
    """A walk that reaches every slice but enumerates a different population each
    time is not covering the portal, it is sampling it — and that difference is
    invisible in a coverage percentage."""
    conn = _Conn(_full(), candidates=3000, history=_covered_history(2, candidates=1000))
    row = cg.evaluate(conn, "idnes", 2, dry_run=False)
    assert row["covered"] is True
    assert row["verdict"] == "hold"
    assert "moved more than" in row["note"]
    assert conn.unparked is False


def test_ordinary_churn_does_not_reset_the_streak() -> None:
    """The tolerance has to admit real movement or the gate never opens at all."""
    conn = _Conn(_full(), candidates=1080, history=_covered_history(2, candidates=1000))
    row = cg.evaluate(conn, "idnes", 2, dry_run=False)
    assert row["verdict"] == "unparked"


def test_an_empty_ledger_keeps_the_flag_down() -> None:
    """The state every portal starts in. No evidence is not weak evidence."""
    conn = _Conn([], history=_covered_history(3))
    row = cg.evaluate(conn, "idnes", 10, dry_run=False)
    assert row["covered"] is False
    assert conn.unparked is False


def test_an_uninstrumented_portal_says_so_rather_than_looking_broken() -> None:
    """"No ledger at all" and "walked and came up short" are different
    statements, and only idnes writes the ledger today — so ceskereality and
    mmreality will sit at hold. Reporting that as "0/12 categories covered"
    reads as a broken walk; it is an absent instrument."""
    conn = _Conn([], history=[])
    row = cg.evaluate(conn, "ceskereality", 12, dry_run=False)
    assert row["verdict"] == "hold"
    assert "no slice ledger" in row["note"]


def test_a_portal_declaring_no_categories_can_never_pass() -> None:
    """Guards the vacuous-truth hole: with zero declared categories, "every
    category is covered" is trivially true and the gate would open on nothing."""
    conn = _Conn([], history=_covered_history(3))
    row = cg.evaluate(conn, "idnes", 0, dry_run=False)
    assert row["covered"] is False
    assert conn.unparked is False


def test_dry_run_decides_but_never_acts() -> None:
    conn = _Conn(_full(), history=_covered_history(2))
    row = cg.evaluate(conn, "idnes", 2, dry_run=True)
    assert row["verdict"] == "pass"          # it would have opened…
    assert conn.unparked is False            # …but touched nothing
    assert not any("insert into portal_coverage_gate" in s for s, _ in conn.executed)


def test_every_evaluation_is_recorded_not_just_the_passing_ones() -> None:
    """A verdict that only exists in an expiring Actions log is a verdict nobody
    receives — and the holds are the interesting ones while a portal is parked."""
    conn = _Conn([("byt", "prodej", 15, 3)], history=[])
    cg.evaluate(conn, "idnes", 2, dry_run=False)
    inserts = [p for s, p in conn.executed if "insert into portal_coverage_gate" in s]
    assert len(inserts) == 1
    assert inserts[0]["verdict"] == "hold"
    assert inserts[0]["covered"] is False


def test_the_gate_only_ever_looks_at_parked_portals() -> None:
    """It can open a gate; it must never be able to close one. A portal that is
    already delisting is none of its business."""
    src = (cg.__file__)
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "supports_complete_walk = false" in body   # the selection predicate
    assert "supports_complete_walk = true" in body    # the only write
    assert body.count("update portals set") == 1
