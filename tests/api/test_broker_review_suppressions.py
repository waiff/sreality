"""The unmerge/dismiss suppression rail (migration 401) — hermetic, no DB.

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
        self.rowcount = 0

    def __enter__(self) -> "_Cur":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        s = " ".join(sql.split())
        self._conn.executed.append((s, params))
        self._rows = [self._shape(r) for r in self._conn.rows_for(s, params)]
        self.rowcount = self._conn.rowcount_for(s, params, len(self._rows))

    def _shape(self, row: Any) -> Any:
        return row

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Conn:
    """Serves the fixed row sets each statement of the flow expects.

    `owner_now` is where each identity CURRENTLY sits (identity id -> broker id) and
    is what both cohort reads are served from — the query the code runs is anchored on
    a broker set, never on the survivor recorded on the event rows."""

    def __init__(self, **fixtures: Any) -> None:
        self.executed: list[tuple[str, Any]] = []
        self.group_row: Any = fixtures.get("group_row")
        self.events: list[tuple[int, int]] = fixtures.get("events", [])
        self.owner_now: dict[int, int] = fixtures.get("owner_now", {})
        self.meta: dict[int, tuple[str, str, str | None]] = fixtures.get("meta", {})
        self.candidate: dict[str, Any] | None = fixtures.get("candidate")
        self.active_brokers: list[int] = fixtures.get("active_brokers", [])
        self.suppression_conflicts: int = fixtures.get("suppression_conflicts", 0)
        self.suppression_row: Any = fixtures.get("suppression_row")
        self.suppression_list: list[Any] = fixtures.get("suppression_list", [])

    def _cohort(self, brokers: set[int]) -> list[Any]:
        return [(i, *self.meta[i], self.owner_now[i])
                for i in sorted(self.meta)
                if self.owner_now.get(i) in brokers]

    def rows_for(self, sql: str, params: Any) -> list[Any]:
        if "array_agg(DISTINCT retired_broker_id)" in sql:
            return [self.group_row] if self.group_row else []
        if "SELECT identity_id, prev_broker_id FROM broker_merge_events" in sql:
            return list(self.events)
        if "SELECT id, source, source_broker_id_native" in sql:
            # the two cohort reads: one anchored on where the restored identities
            # currently live, one on the candidate's broker pair
            if "restored" in (params or {}):
                anchors = {self.owner_now[i] for i in params["restored"]
                           if i in self.owner_now}
            else:
                anchors = set(params["brokers"])
            return self._cohort(anchors)
        if "UPDATE broker_merge_candidates SET status='dismissed'" in sql:
            return [self.candidate] if self.candidate else []
        if "SELECT id FROM brokers WHERE id = ANY" in sql:
            return [(b,) for b in self.active_brokers]
        if "SELECT id, lifted_at FROM broker_merge_suppressions" in sql:
            return [self.suppression_row] if self.suppression_row else []
        if "UPDATE broker_merge_suppressions SET lifted_at" in sql:
            return [(params["id"],)]
        if "FROM broker_merge_suppressions s" in sql:
            return list(self.suppression_list)
        return []

    def rowcount_for(self, sql: str, params: Any, rows: int) -> int:
        if "INSERT INTO broker_merge_suppressions" in sql:
            # ON CONFLICT DO NOTHING skips an already-active pair, so rowcount is
            # the pairs INSERTED, which is what the operator is told was recorded.
            return max(0, len(params["lo"]) - self.suppression_conflicts)
        return rows

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
    assert review.suppression_pairs({1: 10, 2: 10, 3: 20}) == [(1, 3), (2, 3)]


def test_suppression_pairs_now_cover_same_source_pairs_too() -> None:
    """They used to be dropped: the engine refused a within-source merge, so such a
    row could never fire. That policy is repealed (2026-08-20) and the biggest
    duplicate fans are same-portal — dropping them now would mean an unmerged
    within-portal group comes straight back on the next sweep."""
    assert review.suppression_pairs({1: 10, 2: 20}) == [(1, 2)]


def test_suppression_pairs_are_normalized_lo_before_hi() -> None:
    """The sweep looks the pair up the way decide_merges normalises one, and the
    partial UNIQUE index is on (identity_lo, identity_hi) — an unnormalized row
    would be both un-matchable and duplicable."""
    pairs = review.suppression_pairs({9: 10, 4: 20})
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
        owner_now={1: 10, 2: 10, 3: 10},
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
    restored and the remaining identities are indistinguishable — the cohort has to
    be read while one broker still holds both."""
    order = [s for s, _ in _run_unmerge().executed]

    def at(needle: str) -> int:
        return next(i for i, s in enumerate(order) if needle in s)

    repoint = at("SET broker_id = ev.prev_broker_id")
    assert at("SELECT identity_id, prev_broker_id FROM broker_merge_events") < repoint
    assert at("SELECT id, source, source_broker_id_native") < repoint
    assert at("INSERT INTO broker_merge_suppressions") < repoint


def test_unmerge_reads_the_cohort_from_where_the_identities_are_now() -> None:
    """THE reachable no-op: survivor = min(id), so a second merge can retire the
    survivor of the first one — and `list_recent_merges` still shows that group with
    an Unmerge button. Deriving the cohort from the survivor recorded on the EVENT
    rows then returns nothing (it holds no identities any more), every identity reads
    as one owner, zero suppressions are written and the sweep re-applies the merge
    that night. The anchor has to be where the restored identities live NOW."""
    conn = _Conn(
        group_row=(10, [20]),
        events=[(2, 20)],
        # broker 10 was itself merged away into 30 after group 'g' was created, so
        # every identity — the restored one included — now sits on broker 30.
        owner_now={1: 30, 2: 30, 3: 30},
        meta={1: ("sreality", "s-1", "Jan"), 2: ("idnes", "i-2", "Jan"),
              3: ("remax", "r-3", "Jan")},
    )
    out = review.unmerge_group(conn, "g", undone_by="op@example.com")
    params = _suppression_params(conn)
    # identity 2 goes back to broker 20; 1 and 3 stay on the CURRENT owner (30)
    assert list(zip(params["lo"], params["hi"])) == [(1, 2), (2, 3)]
    assert out["suppressions_written"] == 2 and out["suppression_note"] is None
    anchored = _stmt(conn, "SELECT id, source, source_broker_id_native")
    assert anchored[1] == {"restored": [2]}


def test_unmerge_records_a_same_portal_separation() -> None:
    """The regression the portal-agnostic engine creates: two sreality identities
    the operator just pulled apart CAN auto-merge back tonight, so the NO has to be
    on record. This used to write nothing at all."""
    conn = _Conn(group_row=(10, [20]), events=[(2, 20)], owner_now={1: 10, 2: 10},
                 meta={1: ("sreality", "s-1", "A"), 2: ("sreality", "s-2", "B")})
    out = review.unmerge_group(conn, "g", undone_by="op@example.com")
    params = _suppression_params(conn)
    assert list(zip(params["lo"], params["hi"])) == [(1, 2)]
    assert out["suppressions_written"] == 1 and out["suppression_note"] is None


def test_an_unmerge_that_derives_nothing_says_so(caplog: Any) -> None:
    """A rail failing open has to be visible: the merge comes back on the next sweep
    and the operator is otherwise never told. Both the log and the response carry it."""
    import logging

    # the restored identity lands back on the broker it already shares with the
    # other one, so nothing was actually separated
    conn = _Conn(group_row=(10, [20]), events=[(2, 10)], owner_now={1: 10, 2: 10},
                 meta={1: ("sreality", "s-1", "A"), 2: ("idnes", "i-2", "B")})
    with caplog.at_level(logging.WARNING, logger="broker_review"):
        out = review.unmerge_group(conn, "g", undone_by="op@example.com")
    assert out["suppressions_written"] == 0
    assert "no cross-owner identity pair" in out["suppression_note"]
    assert "derived 0 suppressions" in caplog.text


def test_a_normal_unmerge_carries_no_warning_note() -> None:
    assert _run_unmerge_result()["suppression_note"] is None


def _run_unmerge_result() -> Any:
    return review.unmerge_group(_unmerge_conn(), "g", undone_by="op@example.com")


def test_suppressions_written_counts_inserted_rows_not_attempted_pairs() -> None:
    """ON CONFLICT DO NOTHING skips a pair that is already suppressed. Reporting the
    attempt would tell the operator this action recorded a decision when it did not
    (unmerge -> operator merge -> unmerge is a legal loop, and two overlapping merge
    groups can name the same pair)."""
    conn = _unmerge_conn()
    conn.suppression_conflicts = 1
    out = review.unmerge_group(conn, "g", undone_by="op@example.com")
    assert len(_suppression_params(conn)["lo"]) == 2   # two pairs attempted...
    assert out["suppressions_written"] == 1            # ...one was already on record


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


def _dismiss_conn(reason: str, evidence: Any, **over: Any) -> _Conn:
    fixtures: dict[str, Any] = {
        "candidate": {"id": 7, "status": "dismissed", "reason": reason,
                      "evidence": evidence, "broker_ids": [10, 20]},
        "meta": {1: ("sreality", "s-1", "Jan"), 2: ("remax", "r-2", "Jan")},
        "owner_now": {1: 10, 2: 20},
    }
    fixtures.update(over)
    return _Conn(**fixtures)


def test_dismissing_a_contact_bridge_candidate_records_the_no() -> None:
    """Dismissal alone only blocks RE-PROPOSAL of the review row; the auto-merge
    path never read it, so the pair still merged as soon as a second bridge value
    appeared or the names converged."""
    conn = _dismiss_conn("contact_bridge_review",
                         {"identity_ids": [2, 1], "bridges": ["email:a@x.cz"]})
    out = review.dismiss_candidate(conn, 7, resolved_by="op@example.com")
    assert out == {"id": 7, "status": "dismissed", "suppressions_written": 1}
    params = _suppression_params(conn)
    assert params["lo"] == [1] and params["hi"] == [2]        # normalized lo<hi
    assert params["origin"] == "dismiss" and params["candidate"] == 7
    assert params["by"] == "op@example.com" and params["group"] is None


def test_a_dismissal_suppresses_every_pair_between_the_two_brokers() -> None:
    """The card is keyed `contactbridge:{lo}:{hi}` — on the BROKER pair — and
    _queue_review_pairs last-write-wins the evidence when several identity pairs
    resolve to the same two brokers. Suppressing only `evidence.identity_ids` would
    leave the sibling pairs live, and the two brokers auto-merge through one of them
    on the very next sweep: the operator's NO was about these two brokers."""
    conn = _dismiss_conn(
        "contact_bridge_review", {"identity_ids": [1, 2]},
        meta={1: ("sreality", "s-1", "Jan"), 2: ("remax", "r-2", "Jan"),
              3: ("idnes", "i-3", "Jan"), 4: ("remax", "r-4", "Jan")},
        owner_now={1: 10, 3: 10, 2: 20, 4: 20})
    out = review.dismiss_candidate(conn, 7, resolved_by="op@example.com")
    params = _suppression_params(conn)
    # every cross-broker, cross-SOURCE pair — including (3, 4), which no evidence
    # blob ever named; (2, 4) is same-broker and (nothing here) same-source
    assert list(zip(params["lo"], params["hi"])) == [(1, 2), (1, 4), (2, 3), (3, 4)]
    assert out["suppressions_written"] == 4
    assert _stmt(conn, "SELECT id, source, source_broker_id_native")[1] == {
        "brokers": [10, 20]}


def test_dismissing_after_the_brokers_already_merged_writes_nothing() -> None:
    """The race: a proposed candidate outlives its brokers being merged (candidates
    are retired later in the sweep, and an operator merge via /merge never stamps the
    row). Suppressing then would write an ACTIVE row whose identities ALREADY share a
    broker — an instant, permanent verify_pipeline violation with nothing to undo it."""
    conn = _dismiss_conn("contact_bridge_review", {"identity_ids": [1, 2]},
                         owner_now={1: 10, 2: 10})
    out = review.dismiss_candidate(conn, 7, resolved_by="op@example.com")
    assert out == {"id": 7, "status": "dismissed", "suppressions_written": 0}
    assert _suppression_params(conn) is None


def test_a_candidate_without_a_readable_broker_pair_writes_nothing() -> None:
    for broker_ids in (None, [], [10], "10,20", [10, "x"]):
        conn = _dismiss_conn("contact_bridge_review", {"identity_ids": [1, 2]})
        conn.candidate = dict(conn.candidate or {}, broker_ids=broker_ids)
        assert review.dismiss_candidate(conn, 7)["suppressions_written"] == 0
        assert _suppression_params(conn) is None


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
    conn = _Conn(meta={1: ("sreality", "s-1", "Jan")})
    assert review._write_suppressions(conn, [(1, 99)], {1: ("sreality", "s-1", "Jan")},
                                      origin="dismiss") == 0
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


def test_the_lift_only_covers_pairs_the_merge_actually_brings_together() -> None:
    """`broker_id = ANY(ids)` on both ends also matches a pair ALREADY co-located
    under one of the merged brokers — i.e. a violation of the invariant. Lifting that
    row would silently erase the evidence of the bypass instead of overruling a
    decision, because this merge never adjudicated that pair at all."""
    sql = " ".join(review._SUPPRESSION_LIFT_SQL.split())
    assert "lo.broker_id <> hi.broker_id" in sql


def test_the_lift_runs_before_the_identities_move() -> None:
    conn = _Conn(active_brokers=[10, 20])
    review.merge_brokers(conn, [10, 20], created_by="op@example.com")
    order = [s for s, _ in conn.executed]
    lift = next(i for i, s in enumerate(order) if "broker_merge_suppressions" in s)
    move = next(i for i, s in enumerate(order) if "UPDATE broker_identities SET broker_id" in s)
    assert lift < move


# --- the statement plan itself -------------------------------------------------


def test_the_suppression_insert_pins_every_column_to_its_projection() -> None:
    """source_lo/native_lo/source_hi/native_hi are four same-typed text columns fed
    from four same-typed arrays. Swapping any two survives pytest AND the PREPARE
    gate (identical types, identical arity) and writes a natural key that names the
    wrong portal for the rest of the row's life. The only guard is the text itself."""
    sql = " ".join(review._SUPPRESSION_INSERT_SQL.split())
    columns = ("identity_lo, identity_hi, source_lo, native_lo, source_hi, native_hi, "
               "display_lo, display_hi, origin, merge_group_id, candidate_id, created_by)")
    projection = "SELECT d.lo, d.hi, d.slo, d.nlo, d.shi, d.nhi, d.dlo, d.dhi,"
    unnest = ("FROM unnest(%(lo)s::bigint[], %(hi)s::bigint[], %(slo)s::text[], "
              "%(nlo)s::text[], %(shi)s::text[], %(nhi)s::text[], %(dlo)s::text[], "
              "%(dhi)s::text[]) AS d(lo, hi, slo, nlo, shi, nhi, dlo, dhi)")
    assert columns in sql and projection in sql and unnest in sql
    assert sql.index(columns) < sql.index(projection) < sql.index(unnest)


def test_the_origin_literals_match_the_migration_check_constraint() -> None:
    """A fake conn enforces no CHECK (see [[adversarial-review-fake-conn-db-constraints]]),
    so pin the literals against the migration that declares them — the same guard
    that caught merge_brokers writing source='manual' against migration 186."""
    import pathlib
    import re

    module = pathlib.Path(review.__file__).read_text()
    written = set(re.findall(r'origin="(\w+)"', module))
    migration = (pathlib.Path(__file__).resolve().parents[2]
                 / "migrations/401_broker_merge_suppressions.sql").read_text()
    allowed = set(re.search(r"CHECK \(origin IN \(([^)]*)\)\)", migration)[1]
                  .replace("'", "").replace(" ", "").split(","))
    assert written and written <= allowed, (written, allowed)


# --- the ledger: list + manual lift --------------------------------------------


def test_listing_suppressions_puts_active_rows_first_and_pages() -> None:
    """Active rows are the operational set (they gate tonight's sweep); lifted rows
    are history that must stay readable — never deleted (rule #3)."""
    sql = " ".join(review._SUPPRESSION_LIST_SQL.split())
    assert "ORDER BY (s.lifted_at IS NOT NULL), s.created_at DESC, s.id DESC" in sql
    assert "WHERE (%(include_lifted)s::boolean OR s.lifted_at IS NULL)" in sql

    conn = _Conn(suppression_list=[
        {"id": 1, "identity_lo": 1, "identity_hi": 2, "origin": "unmerge",
         "merge_group_id": "g", "created_at": None, "lifted_at": None},
    ])
    out = review.list_suppressions(conn, limit=10, offset=5, include_lifted=True)
    assert out["count"] == 1 and out["suppressions"][0]["active"] is True
    params = _stmt(conn, "FROM broker_merge_suppressions s")[1]
    assert params == {"include_lifted": True, "limit": 10, "offset": 5}


def test_lifting_a_suppression_stamps_who_and_why_without_deleting() -> None:
    conn = _Conn(suppression_row=(5, None))
    out = review.lift_suppression(conn, 5, lifted_by="op@example.com",
                                  reason="same person after all")
    assert out == {"id": 5, "lifted": True, "lift_reason": "same person after all",
                   "lifted_by": "op@example.com"}
    sql, params = _stmt(conn, "UPDATE broker_merge_suppressions SET lifted_at")
    assert params == {"id": 5, "by": "op@example.com", "reason": "same person after all"}
    assert "lifted_at IS NULL" in sql and "DELETE" not in sql.upper()


def test_lifting_without_a_reason_still_records_one() -> None:
    conn = _Conn(suppression_row=(5, None))
    assert review.lift_suppression(conn, 5)["lift_reason"] == "operator_lift"


def test_lifting_an_unknown_suppression_is_not_found() -> None:
    assert review.lift_suppression(_Conn(suppression_row=None), 5) is None


def test_lifting_an_already_lifted_suppression_is_a_conflict() -> None:
    """A second POST must not re-stamp lifted_by/lift_reason over the real one."""
    import pytest

    conn = _Conn(suppression_row=(5, "2026-08-13T00:00:00Z"))
    with pytest.raises(review.MergeError):
        review.lift_suppression(conn, 5, lifted_by="op@example.com")
    assert _stmt(conn, "UPDATE broker_merge_suppressions SET lifted_at") is None


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
