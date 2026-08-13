"""The unmerge/dismiss suppression rail (migration 399) — hermetic, no DB.

The gap these cover (2026-08-12 brokers E2E review, decision D5): the nightly
sweep re-derives its whole cross-source candidate set from broker_identity_contacts
and consulted NO record of a past decision, so an unmerge was undone again the same
night and a dismissed pair auto-merged the moment its evidence strengthened.

`unmerge_group`'s SQL body had zero coverage before this file — both route tests
monkeypatch it away. A fake connection cannot enforce a CHECK, an FK or the partial
UNIQUE index (see [[adversarial-review-fake-conn-db-constraints]]), so these assert
the derivation and the statement plan; the schema-replay job PREPAREs the SQL.
"""

from __future__ import annotations

import contextlib
from typing import Any

from api import broker_review as review


class _Cur:
    def __init__(self, conn: "_Conn", dicts: bool) -> None:
        self._conn, self._dicts = conn, dicts
        self._rows: list[Any] = []

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        self._rows = [self._shape(r) for r in self._conn.rows_for(s, params)]

    def _shape(self, row: Any) -> Any:
        return row

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Conn:
    """Serves the fixed row sets each statement of the flow expects."""

    def __init__(self, **fixtures: Any) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.group_row: Any = fixtures.get("group_row")
        self.events: list[tuple[int, int]] = fixtures.get("events", [])
        self.survivor_identities: list[int] = fixtures.get("survivor_identities", [])
        self.meta: dict[int, tuple[str, str, str | None]] = fixtures.get("meta", {})
        self.candidate: dict[str, Any] | None = fixtures.get("candidate")
        self.active_brokers: list[int] = fixtures.get("active_brokers", [])

    def rows_for(self, sql: str, params: Any) -> list[Any]:
        if "array_agg(DISTINCT retired_broker_id)" in sql:
            return [self.group_row] if self.group_row else []
        if "SELECT identity_id, prev_broker_id FROM broker_merge_events" in sql:
            return list(self.events)
        if "SELECT id FROM broker_identities WHERE broker_id = %(broker)s" in sql:
            return [(i,) for i in self.survivor_identities]
        if "SELECT id, source, source_broker_id_native" in sql:
            wanted = set(params["ids"])
            return [(i, *m) for i, m in sorted(self.meta.items()) if i in wanted]
        if "UPDATE broker_merge_candidates SET status='dismissed'" in sql:
            return [self.candidate] if self.candidate else []
        if "SELECT id FROM brokers WHERE id = ANY" in sql:
            return [(b,) for b in self.active_brokers]
        return []

    def cursor(self, **kw: Any) -> _Cur:
        return _Cur(self, bool(kw.get("row_factory")))

    def transaction(self) -> Any:
        return contextlib.nullcontext()


def _stmt(conn: _Conn, needle: str) -> tuple[str, Any] | None:
    return next((e for e in conn.executed if needle in e[0]), None)


def _suppression_params(conn: _Conn) -> Any:
    found = _stmt(conn, "INSERT INTO broker_merge_suppressions")
    return None if found is None else found[1]


# --- the pure derivation ------------------------------------------------------


def test_suppression_pairs_only_spans_different_owners() -> None:
    """Identities that stayed together are not a decision; only the separation is."""
    owner = {1: 10, 2: 10, 3: 20}
    sources = {1: "sreality", 2: "idnes", 3: "remax"}
    assert review.suppression_pairs(owner, sources) == [(1, 3), (2, 3)]


def test_suppression_pairs_skips_same_source_pairs() -> None:
    """decide_merges refuses a same-source bridge outright ('within a source the
    portal-native id is authoritative'), so a same-source suppression could never
    fire — it would just be a row nothing reads."""
    owner = {1: 10, 2: 20}
    assert review.suppression_pairs(owner, {1: "sreality", 2: "sreality"}) == []
    assert review.suppression_pairs(owner, {1: "sreality", 2: "idnes"}) == [(1, 2)]


def test_suppression_pairs_are_normalized_lo_before_hi() -> None:
    """The sweep looks the pair up as Bridge.pair() spells it, and the partial
    UNIQUE index is on (identity_lo, identity_hi) — an unnormalized row would be
    both un-matchable and duplicable."""
    owner = {9: 10, 4: 20}
    pairs = review.suppression_pairs(owner, {9: "sreality", 4: "idnes"})
    assert pairs == [(4, 9)]
    assert all(lo < hi for lo, hi in pairs)


# --- unmerge ------------------------------------------------------------------


def _unmerge_conn() -> _Conn:
    # merge group 'g': identities 1 (sreality) and 2 (idnes) were pulled onto
    # survivor broker 10 from brokers 10 and 20; identity 3 (remax) was already the
    # survivor's own and stays put.
    return _Conn(
        group_row=(10, [20]),
        events=[(2, 20)],
        survivor_identities=[1, 2, 3],
        meta={1: ("sreality", "s-1", "Jan Novák"),
              2: ("idnes", "i-2", "Jan Novak"),
              3: ("remax", "r-3", "Jan Novak")},
    )


def test_unmerge_suppresses_every_pair_it_pulled_apart() -> None:
    conn = _unmerge_conn()
    out = review.unmerge_group(conn, "g", undone_by="op@example.com")
    assert out["restored_broker_ids"] == [20] and out["suppressions_written"] == 2
    params = _suppression_params(conn)
    # identity 2 goes back to broker 20; 1 and 3 stay on 10 — so both cross-owner
    # pairs are recorded, and the (1, 3) pair that stayed together is not
    assert list(zip(params["lo"], params["hi"])) == [(1, 2), (2, 3)]
    assert params["origin"] == "unmerge"
    assert params["group"] == "g" and params["by"] == "op@example.com"
    assert params["candidate"] is None


def test_unmerge_denormalizes_the_natural_key_of_each_side() -> None:
    """A pair of bare identity ids is unreadable in an audit six months later."""
    params = _suppression_params(_run_unmerge())
    assert params["slo"] == ["sreality", "idnes"] and params["nlo"] == ["s-1", "i-2"]
    assert params["shi"] == ["idnes", "remax"] and params["nhi"] == ["i-2", "r-3"]
    assert params["dlo"] == ["Jan Novák", "Jan Novak"]


def _run_unmerge(conn: _Conn | None = None) -> _Conn:
    conn = conn or _unmerge_conn()
    review.unmerge_group(conn, "g", undone_by="op@example.com")
    return conn


def test_unmerge_derives_ownership_before_the_repoint() -> None:
    """After `UPDATE broker_identities SET broker_id = ev.prev_broker_id` the
    restored and the remaining identities are indistinguishable — the survivor's
    identity list has to be read while it still holds both."""
    order = [s for s, _ in _run_unmerge().executed]

    def at(needle: str) -> int:
        return next(i for i, s in enumerate(order) if needle in s)

    repoint = at("SET broker_id = ev.prev_broker_id")
    assert at("SELECT identity_id, prev_broker_id FROM broker_merge_events") < repoint
    assert at("SELECT id FROM broker_identities WHERE broker_id") < repoint
    assert at("INSERT INTO broker_merge_suppressions") < repoint


def test_unmerge_skips_same_source_pairs() -> None:
    """Two sreality identities separated by an unmerge can never auto-merge back
    (the resolver refuses same-source bridges), so no row is written for them."""
    conn = _Conn(group_row=(10, [20]), events=[(2, 20)], survivor_identities=[1, 2],
                 meta={1: ("sreality", "s-1", "A"), 2: ("sreality", "s-2", "B")})
    review.unmerge_group(conn, "g", undone_by="op@example.com")
    assert _suppression_params(conn) is None


def test_unmerge_still_replays_the_ledger_and_reactivates_the_losers() -> None:
    """The rail is additive: the pre-existing unmerge behaviour must be untouched."""
    conn = _run_unmerge()
    assert _stmt(conn, "SET broker_id = ev.prev_broker_id") is not None
    assert _stmt(conn, "UPDATE brokers SET status='active'") is not None
    undo = _stmt(conn, "UPDATE broker_merge_events SET undone_at=now()")
    assert undo is not None and undo[1] == ("op@example.com", "g")


def test_unmerge_of_an_unknown_group_writes_nothing() -> None:
    conn = _Conn(group_row=None)
    assert review.unmerge_group(conn, "nope") is None
    assert _suppression_params(conn) is None


def test_the_suppression_insert_tolerates_a_re_suppressed_pair() -> None:
    """Unmerge -> operator merge (which lifts) -> unmerge again is a legal loop, and
    two overlapping groups can name the same pair. A bare INSERT would 23505 and the
    operator would see a failed unmerge."""
    assert ("ON CONFLICT (identity_lo, identity_hi) WHERE lifted_at IS NULL DO NOTHING"
            in " ".join(review._SUPPRESSION_INSERT_SQL.split()))


# --- dismiss ------------------------------------------------------------------


def _dismiss_conn(reason: str, evidence: Any) -> _Conn:
    return _Conn(candidate={"id": 7, "status": "dismissed", "reason": reason,
                            "evidence": evidence},
                 meta={1: ("sreality", "s-1", "Jan"), 2: ("remax", "r-2", "Jan")})


def test_dismissing_a_contact_bridge_candidate_records_the_no() -> None:
    """Dismissal alone only blocks RE-PROPOSAL of the review row; the auto-merge
    path never read it, so the pair still merged as soon as a second bridge value
    appeared or the names converged."""
    conn = _dismiss_conn("contact_bridge_review",
                         {"identity_ids": [2, 1], "bridges": ["email:a@x.cz"]})
    out = review.dismiss_candidate(conn, 7, resolved_by="op@example.com")
    assert out == {"id": 7, "status": "dismissed"}
    params = _suppression_params(conn)
    assert params["lo"] == [1] and params["hi"] == [2]        # normalized lo<hi
    assert params["origin"] == "dismiss" and params["candidate"] == 7
    assert params["by"] == "op@example.com" and params["group"] is None


def test_dismissing_a_name_firm_candidate_writes_no_suppression() -> None:
    """A different mechanism: same name + firm, no contact bridge at all. The gate is
    the REASON, not merely the absence of identity ids — a name_firm proposal whose
    evidence happened to carry a pair would still not be a contact-bridge decision,
    and suppressing it would block a bridge the operator never ruled on."""
    conn = _dismiss_conn("name_firm", {"brokers": [1, 2]})
    assert review.dismiss_candidate(conn, 7)["status"] == "dismissed"
    assert _suppression_params(conn) is None

    with_ids = _dismiss_conn("name_firm", {"identity_ids": [1, 2]})
    assert review.dismiss_candidate(with_ids, 7)["status"] == "dismissed"
    assert _suppression_params(with_ids) is None


def test_malformed_evidence_dismisses_without_raising() -> None:
    for evidence in (None, {}, {"identity_ids": None}, {"identity_ids": [1]},
                     {"identity_ids": ["a", "b"]}, {"identity_ids": [5, 5]},
                     "not-a-dict"):
        conn = _dismiss_conn("contact_bridge_review", evidence)
        assert review.dismiss_candidate(conn, 7)["status"] == "dismissed"
        assert _suppression_params(conn) is None


def test_an_identity_missing_from_the_registry_is_skipped_not_faked() -> None:
    """source/native are NOT NULL; a placeholder natural key would be worse than no
    row, because it reads as provenance."""
    conn = _dismiss_conn("contact_bridge_review", {"identity_ids": [1, 99]})
    assert review.dismiss_candidate(conn, 7)["status"] == "dismissed"
    assert _suppression_params(conn) is None


def test_dismissing_an_already_resolved_candidate_is_a_404_not_a_suppression() -> None:
    conn = _Conn(candidate=None)
    assert review.dismiss_candidate(conn, 7) is None
    assert _suppression_params(conn) is None


# --- lift on an explicit operator merge ---------------------------------------


def test_an_operator_merge_lifts_the_suppressions_it_overrides() -> None:
    """The operator always wins over the rail — it gates the AUTO path only. Without
    the lift, verify_pipeline's invariant (an active suppression whose identities
    share a broker) would red on a legitimate override."""
    conn = _Conn(active_brokers=[10, 20])
    out = review.merge_brokers(conn, [10, 20], created_by="op@example.com")
    assert out["survivor_broker_id"] == 10
    lift = _stmt(conn, "UPDATE broker_merge_suppressions")
    assert lift is not None
    assert lift[1] == {"by": "op@example.com", "ids": [10, 20]}
    assert "lift_reason = 'operator_merge'" in lift[0]
    assert "s.lifted_at IS NULL" in lift[0]


def test_the_lift_only_covers_pairs_whose_both_sides_are_being_merged() -> None:
    """Joined to broker_identities on BOTH ends: a suppression with one foot outside
    the merge is a decision this merge does not overrule."""
    sql = " ".join(review._SUPPRESSION_LIFT_SQL.split())
    assert "lo.id = s.identity_lo AND hi.id = s.identity_hi" in sql
    assert sql.count("broker_id = ANY(%(ids)s)") == 2
    assert "DELETE" not in sql.upper()      # lifting is never a delete (rule #3)


def test_the_lift_runs_before_the_identities_move() -> None:
    conn = _Conn(active_brokers=[10, 20])
    review.merge_brokers(conn, [10, 20], created_by="op@example.com")
    order = [s for s, _ in conn.executed]
    lift = next(i for i, s in enumerate(order) if "broker_merge_suppressions" in s)
    move = next(i for i, s in enumerate(order) if "UPDATE broker_identities SET broker_id" in s)
    assert lift < move


def test_merge_candidate_threads_the_operator_through_to_merge_brokers(
    monkeypatch: Any,
) -> None:
    """merge_candidate delegates to merge_brokers, so the lift rides along — but
    only if the identity is actually passed through."""
    captured: dict[str, Any] = {}

    class _CandConn(_Conn):
        def rows_for(self, sql: str, params: Any) -> list[Any]:
            if "FROM broker_merge_candidates WHERE id" in sql:
                return [{"id": 3, "status": "proposed", "broker_ids": [10, 20],
                         "reason": "name_firm"}]
            return super().rows_for(sql, params)

    monkeypatch.setattr(review, "merge_brokers",
                        lambda conn, ids, **kw: captured.update(kw) or {"ok": True})
    conn = _CandConn(active_brokers=[10, 20])
    review.merge_candidate(conn, 3, created_by="op@example.com")
    assert captured["created_by"] == "op@example.com"
    resolved = _stmt(conn, "UPDATE broker_merge_candidates SET status='merged'")
    assert resolved is not None and resolved[1] == ("op@example.com", 3)
