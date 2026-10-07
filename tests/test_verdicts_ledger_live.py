"""Migration 574 executed (E920): the operator's rulings are a ledger, and every reader obeys the
newest row. A flip and a withdrawal are NEW rows; the lane's must-links (`RT_MUST_LINK_SQL`), the
apply path's negatives (`apply_sql.PAIR_VERDICTS_SQL`, `Negatives.read` over group rulings) and
the rulings page's own reads all take the newest word per pair / per set. The writes also hold on
a store the migration has not reached (the pre-574 unique indexes, re-created inside the test's
transaction). Runs in CI's migrations job (`TEST_DATABASE_URL`); every test rolls back.
"""

from __future__ import annotations

import itertools
import os
import uuid
from typing import Any

import pytest

from autodedup import apply_sql
from autodedup import incremental_sql
from autodedup import ui_sql as usql
from autodedup.apply import Negatives
from toolkit.property_identity import record_ruling

_DB_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="TEST_DATABASE_URL not set — schema-replay test runs only in the CI DB job",
)

OP = "ci-operator@replay.local"
_SREALITY_IDS = itertools.count(9_400_000_001)


@pytest.fixture()
def cur():
    import psycopg

    conn = psycopg.connect(
        _DB_URL,
        options="-c statement_timeout=20000 -c lock_timeout=5000"
        " -c idle_in_transaction_session_timeout=30000",
    )
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


def _property(cur: Any) -> int:
    cur.execute("INSERT INTO properties DEFAULT VALUES RETURNING id")
    return int(cur.fetchone()[0])


def _advert(cur: Any, pid: int, *, source: str = "sreality") -> int:
    cur.execute(
        "INSERT INTO listings (sreality_id, source, source_id_native, raw_json, "
        "category_main, category_type, price_czk, area_m2, is_active, property_id) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'byt', 'prodej', 5000000, 70, true, %s) RETURNING id",
        (next(_SREALITY_IDS) if source == "sreality" else None, source,
         f"ledger-{uuid.uuid4()}", pid),
    )
    return int(cur.fetchone()[0])


def _pair(cur: Any) -> tuple[int, int]:
    a = _advert(cur, _property(cur))
    b = _advert(cur, _property(cur), source="idnes")
    return min(a, b), max(a, b)


def _rows(cur: Any, lo: int, hi: int) -> list[tuple[str, str | None]]:
    cur.execute("SELECT verdict, note FROM autodedup.verdicts WHERE kind = 'pair' "
                "AND listing_lo = %s AND listing_hi = %s ORDER BY decided_at, id", (lo, hi))
    return [(str(v), n) for v, n in cur.fetchall()]


def _must_links(cur: Any) -> set[tuple[int, int]]:
    cur.execute(incremental_sql.RT_MUST_LINK_SQL)
    return {(int(lo), int(hi)) for lo, hi in cur.fetchall()}


def _negatives(cur: Any, lo: int, hi: int) -> set[tuple[int, int]]:
    cur.execute(apply_sql.PAIR_VERDICTS_SQL, {
        "listing_ids": [lo, hi], "negatives": list(usql.NEGATIVE_VERDICTS)})
    return {(int(a), int(b)) for a, b, _v in cur.fetchall()}


def _veto(cur: Any, lo: int, hi: int) -> bool:
    cur.execute("SELECT 1 FROM autodedup.must_not_link WHERE listing_lo = %s "
                "AND listing_hi = %s AND source = 'operator'", (lo, hi))
    return cur.fetchone() is not None


def _rule(cur: Any, lo: int, hi: int, verdict: str, note: str | None = None,
          by: str = OP) -> tuple[Any, ...] | None:
    return record_ruling(cur.connection, lo, hi, verdict=verdict, decided_by=by, note=note)


def test_a_flip_and_a_withdrawal_are_new_rows_and_the_newest_one_binds(cur):
    lo, hi = _pair(cur)
    stored = _rule(cur, lo, hi, "same")
    assert stored is not None and stored[usql.VERDICT_COLUMNS.index("verdict")] == "same"
    # Saying the same thing again appends nothing and answers the standing row.
    again = _rule(cur, lo, hi, "same")
    assert again is not None and again[0] == stored[0]
    assert _rows(cur, lo, hi) == [("same", None)]
    assert (lo, hi) in _must_links(cur)

    _rule(cur, lo, hi, "unsure", "nejsem si jistý")
    assert _rows(cur, lo, hi) == [("same", None), ("unsure", "nejsem si jistý")]
    assert (lo, hi) not in _must_links(cur), "a withdrawn same still binds the lane"
    assert not _negatives(cur, lo, hi)

    _rule(cur, lo, hi, "different", "jiné patro")
    assert [v for v, _ in _rows(cur, lo, hi)] == ["same", "unsure", "different"]
    assert _negatives(cur, lo, hi) == {(lo, hi)}
    assert _veto(cur, lo, hi)
    assert (lo, hi) not in _must_links(cur)

    # A flip back by ANOTHER decider still wins: the newest word, whoever said it.
    _rule(cur, lo, hi, "same", by="operator")
    assert not _negatives(cur, lo, hi)
    assert not _veto(cur, lo, hi)
    assert (lo, hi) in _must_links(cur)
    cur.execute(usql.PAIR_NEWEST_RULING_SQL, {"listing_lo": lo, "listing_hi": hi})
    newest = cur.fetchone()
    assert newest[usql.VERDICT_COLUMNS.index("verdict")] == "same"
    assert newest[usql.VERDICT_COLUMNS.index("decided_by")] == "operator"


def test_a_group_correction_appends_on_its_set_and_apply_reads_the_newest(cur):
    a, b = _pair(cur)

    def say(verdict: str, by: str = OP) -> None:
        cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
            "cluster_key": a, "verdict": verdict, "note": None, "reasons": [],
            "decided_by": by, "generation": "g-ledger", "member_ids": [a, b]})

    def refused() -> bool:
        return bool(Negatives.read(cur.connection, [a, b], [a]).sets.get(a))

    say("different")
    assert refused()
    say("same", by="someone.else@replay.local")
    assert not refused(), "an older negative by another operator outlived the newer same"
    say("different")
    assert refused()
    say("unsure")
    assert not refused(), "a withdrawn group negative still refuses"
    cur.execute("SELECT count(*) FROM autodedup.verdicts WHERE kind = 'cluster' "
                "AND cluster_key = %s", (a,))
    assert cur.fetchone() == (4,)
    cur.execute(usql.CLUSTER_NEWEST_RULING_SQL, {"cluster_key": a, "generation": "g-ledger"})
    assert cur.fetchone()[usql.VERDICT_COLUMNS.index("verdict")] == "unsure"


def test_a_withdrawn_setless_group_ruling_no_longer_refuses_its_key(cur):
    """A ruling taken before 538 recorded no set; the page offers only its withdrawal, which
    copies the NULL set. The newest setless row of the key stands (E920 iii)."""
    a, b = _pair(cur)

    def say(verdict: str) -> None:
        cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
            "cluster_key": a, "verdict": verdict, "note": None, "reasons": [],
            "decided_by": OP, "generation": None, "member_ids": None})

    def refused() -> bool:
        return a in Negatives.read(cur.connection, [a, b], [a]).setless_keys

    say("different")
    assert refused()
    say("unsure")
    assert not refused(), "a withdrawn setless ruling still refuses its key"
    say("different")
    assert refused()


def test_the_writes_hold_on_a_store_574_has_not_reached(cur):
    """The code ships before the migration is applied: with the pre-574 unique indexes back,
    a same-decider re-ruling updates that decider's row in place and never raises."""
    cur.execute("CREATE UNIQUE INDEX pre574_pair_uidx ON autodedup.verdicts "
                "(kind, listing_lo, listing_hi, decided_by) WHERE kind = 'pair'")
    cur.execute("CREATE UNIQUE INDEX pre574_cluster_uidx ON autodedup.verdicts "
                "(kind, cluster_key, (coalesce(generation, ''::text)), decided_by) "
                "WHERE kind = 'cluster'")
    lo, hi = _pair(cur)
    _rule(cur, lo, hi, "same")
    flipped = _rule(cur, lo, hi, "different", "jiné patro")
    assert flipped is not None and flipped[usql.VERDICT_COLUMNS.index("verdict")] == "different"
    assert _rows(cur, lo, hi) == [("different", "jiné patro")]
    _rule(cur, lo, hi, "same", by="operator")
    assert [v for v, _ in _rows(cur, lo, hi)] == ["different", "same"]
    for verdict in ("same", "unsure"):
        cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
            "cluster_key": lo, "verdict": verdict, "note": None, "reasons": [],
            "decided_by": OP, "generation": "g-pre", "member_ids": [lo, hi]})
        assert cur.fetchone()[usql.VERDICT_COLUMNS.index("verdict")] == verdict
    cur.execute("SELECT count(*) FROM autodedup.verdicts WHERE kind = 'cluster' "
                "AND cluster_key = %s", (lo,))
    assert cur.fetchone() == (1,)


def _params(**over: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "generation": "g-ledger-none", "verdict": None, "source": None, "status": None,
        "engine": None, "together": None, "decided_from": None, "decided_to": None,
        "obec": None, "cast_obce": None, "listing": None, "property": None,
        "merge_group": None,
    }
    params.update(over)
    return params


def _page(cur: Any, sql: str, columns: tuple[str, ...], **over: Any) -> list[dict[str, Any]]:
    params = _params(**over)
    params.update({"after_at": None, "after_lo": None, "after_hi": None, "after_key": None,
                   "limit": 50})
    cur.execute(sql, params)
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def test_the_rulings_page_lists_every_source_with_its_status(cur):
    lo, hi = _pair(cur)
    _rule(cur, lo, hi, "same")
    _rule(cur, lo, hi, "unsure")
    (row,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=lo)
    assert (row["listing_lo"], row["listing_hi"]) == (lo, hi)
    assert (row["source"], row["status"], row["verdict"], row["n_rows"]) == (
        "pair", "withdrawn", "unsure", 2)
    assert (row["together_now"], row["engine_view"], row["agreement"]) == (
        False, "unseen", "none")

    # A group confirmed `same` implies its member pairs; a veto with no ruling is listed too.
    c, d = _pair(cur)
    cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
        "cluster_key": c, "verdict": "same", "note": None, "reasons": [], "decided_by": OP,
        "generation": "g-ledger", "member_ids": [c, d]})
    (implied,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=c)
    assert (implied["source"], implied["ruling_kind"], implied["group_cluster_key"]) == (
        "implied", "cluster", c)
    # Same, but apart now: the page says the ruling is not reflected in production.
    assert implied["agreement"] == "disagrees"
    e, f = _pair(cur)
    cur.execute(usql.MUST_NOT_LINK_UPSERT_SQL, {"listing_lo": e, "listing_hi": f,
                                                "reason": "operator: different"})
    (veto,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=e)
    assert (veto["source"], veto["ruling_id"], veto["must_not_link"]) == (
        "must_not_link", None, "operator")

    cur.execute(usql.RULINGS_PAIR_FACETS_SQL, _params(listing=lo))
    facets = {(facet, value): n for facet, value, n in cur.fetchall()}
    assert facets[("total", None)] == 1
    assert facets[("status", "withdrawn")] == 1

    (group,) = _page(cur, usql.RULINGS_GROUP_SQL, usql.RULING_GROUP_COLUMNS, listing=c)
    assert (group["source"], group["set_recorded"], group["n_members"], group["n_properties"],
            group["status"], group["agreement"]) == ("group", True, 2, 2, "standing",
                                                       "disagrees")
    cur.execute(usql.RULINGS_GROUP_FACETS_SQL, _params(listing=c))
    assert ("total", None, 1) in [tuple(r) for r in cur.fetchall()]
    cur.execute(usql.RULING_TOWNS_SQL, {"limit": 40})
    cur.fetchall()


def test_a_set_ruled_again_under_another_key_implies_nothing_and_supersedes(cur):
    """E920 iii at the page: apply reads the newest ruling per SET, whichever key or pass took
    it. {c, d} confirmed `same` under g4's key and later ruled `different` under g13's: the page
    must list no implied `same` for (c, d), and the g4 row is `superseded`, not standing."""
    c, d = _pair(cur)

    def group(key: int, generation: str, verdict: str, members: list[int]) -> None:
        cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
            "cluster_key": key, "verdict": verdict, "note": None, "reasons": [],
            "decided_by": OP, "generation": generation, "member_ids": members})

    group(c, "g-set-4", "same", [c, d])
    (implied,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=c)
    assert (implied["source"], implied["status"]) == ("implied", "standing")
    group(d, "g-set-13", "different", [d, c])
    assert _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=c) == [], (
        "a set ruled different later, under another key, still implies a standing same")
    rows = {(r["cluster_key"], r["generation"]): r
            for r in _page(cur, usql.RULINGS_GROUP_SQL, usql.RULING_GROUP_COLUMNS, listing=c)}
    assert rows[(c, "g-set-4")]["status"] == "superseded"
    assert rows[(c, "g-set-4")]["agreement"] == "none"
    assert rows[(d, "g-set-13")]["status"] == "standing"
    cur.execute(usql.CLUSTER_SET_NEWEST_RULING_SQL, {"member_ids": [c, d]})
    newest = cur.fetchone()
    assert (newest[usql.VERDICT_COLUMNS.index("cluster_key")],
            newest[usql.VERDICT_COLUMNS.index("verdict")]) == (d, "different")
    assert Negatives.read(cur.connection, [c, d], [c]).sets.get(min(c, d)), (
        "and apply refuses the set, which is what the page now says")


def test_correcting_a_bare_veto_keeps_it_in_the_history(cur):
    """A veto with no ruling behind it is the operator's `different`: a flip or a withdrawal first
    writes it down (its reason, its date), so the pair reads `withdrawn`, not `unsure`, and the
    veto's word survives the retraction."""
    lo, hi = _pair(cur)
    cur.execute(usql.MUST_NOT_LINK_UPSERT_SQL, {"listing_lo": lo, "listing_hi": hi,
                                                "reason": "operator split: different"})
    cur.execute("SELECT created_at FROM autodedup.must_not_link WHERE listing_lo = %s "
                "AND listing_hi = %s", (lo, hi))
    vetoed_at = cur.fetchone()[0]
    _rule(cur, lo, hi, "unsure", "nejsem si jistý")
    assert _rows(cur, lo, hi) == [("different", "operator split: different"),
                                  ("unsure", "nejsem si jistý")]
    cur.execute("SELECT decided_at, decided_by FROM autodedup.verdicts WHERE kind = 'pair' "
                "AND listing_lo = %s AND listing_hi = %s AND verdict = 'different'", (lo, hi))
    assert cur.fetchone() == (vetoed_at, "operator")
    assert not _veto(cur, lo, hi)
    (row,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=lo)
    assert (row["source"], row["status"], row["n_rows"]) == ("pair", "withdrawn", 2)
    # A pair that already carries a ruling writes no second copy of its veto.
    _rule(cur, lo, hi, "different", "jiné patro")
    _rule(cur, lo, hi, "same")
    assert [v for v, _ in _rows(cur, lo, hi)] == ["different", "unsure", "different", "same"]


# ------------------------------------------ the engine's view, over a real generation's rows

GEN = "g-ledger-engine"


def _one_property(cur: Any) -> tuple[int, int]:
    pid = _property(cur)
    a = _advert(cur, pid)
    b = _advert(cur, pid, source="idnes")
    return min(a, b), max(a, b)


def _grouped(cur: Any, key: int, *listing_ids: int) -> None:
    for listing_id in listing_ids:
        cur.execute("INSERT INTO autodedup.cluster_members (generation, cluster_key, listing_id) "
                    "VALUES (%s, %s, %s)", (GEN, key, listing_id))


def _read(cur: Any, *listing_ids: int) -> None:
    for listing_id in listing_ids:
        cur.execute("INSERT INTO autodedup.rt_fp (generation, listing_id) VALUES (%s, %s)",
                    (GEN, listing_id))


def _engine(cur: Any, lo: int) -> tuple[str, str, bool]:
    (row,) = _page(cur, usql.RULINGS_PAIR_SQL, usql.RULING_PAIR_COLUMNS, listing=lo,
                   generation=GEN)
    return row["engine_view"], row["agreement"], row["together_now"]


def test_the_engine_view_and_the_agreement_read_a_real_generation(cur):
    # `same`, on one property, one engine group: nothing disagrees.
    a, b = _one_property(cur)
    _rule(cur, a, b, "same")
    _grouped(cur, a, a, b)
    assert _engine(cur, a) == ("together", "agrees", True)
    # `same`, on one property, both read but grouped apart: the engine disagrees.
    c, d = _one_property(cur)
    _rule(cur, c, d, "same")
    _grouped(cur, c, c)
    _read(cur, d)
    assert _engine(cur, c) == ("apart", "disagrees", True)
    # `different`, on two properties, one engine group: the engine disagrees ...
    e, f = _pair(cur)
    _rule(cur, e, f, "different")
    _grouped(cur, e, e, f)
    assert _engine(cur, e) == ("together", "disagrees", False)
    # ... and apart, read: it agrees. Never read at all: unseen, and production alone agrees.
    g, h = _pair(cur)
    _rule(cur, g, h, "different")
    _read(cur, g, h)
    assert _engine(cur, g) == ("apart", "agrees", False)
    i, j = _pair(cur)
    _rule(cur, i, j, "different")
    assert _engine(cur, i) == ("unseen", "agrees", False)
    # The "Neshody" chip is that predicate: exactly the two disagreeing rulings above.
    for lo, expected in ((a, 0), (c, 1), (e, 1), (g, 0), (i, 0)):
        cur.execute(usql.RULINGS_PAIR_FACETS_SQL, _params(listing=lo, generation=GEN,
                                                          engine="disagrees"))
        totals = [n for facet, _value, n in cur.fetchall() if facet == "total"]
        assert (totals[0] if totals else 0) == expected, lo
    # At group grain: a confirmed set the engine holds in one group, on one property.
    cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
        "cluster_key": a, "verdict": "same", "note": None, "reasons": [], "decided_by": OP,
        "generation": GEN, "member_ids": [a, b]})
    (group,) = _page(cur, usql.RULINGS_GROUP_SQL, usql.RULING_GROUP_COLUMNS, listing=a,
                     generation=GEN)
    assert (group["engine_view"], group["n_engine_groups"], group["n_grouped"],
            group["together_now"], group["agreement"]) == ("together", 1, 2, True, "agrees")


def test_the_route_corrects_a_group_ruling_by_copying_its_pass_and_set(cur):
    """`POST /autodedup/verdict` with `supersedes`, called over Postgres: the new row copies the
    old one's generation and member set (E58), and a correction of the ruling it replaced is
    stale (409)."""
    from fastapi import HTTPException

    from api.routes.autodedup import VerdictIn, verdict

    a, b = _pair(cur)
    cur.execute(usql.VERDICT_CLUSTER_APPEND_SQL, {
        "cluster_key": a, "verdict": "different", "note": None, "reasons": [],
        "decided_by": OP, "generation": "g-route", "member_ids": [a, b]})
    old_id = cur.fetchone()[0]
    claims = {"email": OP}
    out = verdict(VerdictIn(kind="cluster", verdict="unsure", note="odvoláno",
                            supersedes=old_id), claims, cur.connection)["data"]
    written = out["verdict"]
    assert (written["kind"], written["cluster_key"], written["generation"],
            written["member_ids"], written["verdict"]) == (
        "cluster", a, "g-route", [a, b], "unsure")
    assert out["superseded"]["id"] == old_id and written["id"] != old_id
    assert not Negatives.read(cur.connection, [a, b], [a]).sets.get(a), (
        "the withdrawn set still refuses at apply")
    with pytest.raises(HTTPException) as stale:
        verdict(VerdictIn(kind="cluster", verdict="same", supersedes=old_id), claims,
                cur.connection)
    assert stale.value.status_code == 409


def test_the_reconciles_history_starts_at_the_generations_last_seed(cur):
    """A9 files a skipped or refused group only when its outcome changed, compared with the
    newest ledger row for the identical member set. Read across seeds, a refusal filed under a
    previous seed silenced every later one (2026-10-07: 94 standing refusals, none visible since
    the 10-06 re-seed). The history now starts at `rt_seed_version:<generation>`'s last write;
    a generation without that key (a batch one) reads its whole ledger."""
    gen = f"t{uuid.uuid4().hex[:8]}"
    members = [9_500_000_001, 9_500_000_002]

    def ledger_row(when_sql: str, run: str) -> None:
        cur.execute(
            "INSERT INTO autodedup.applied_merges (run_id, generation, cluster_key, dry_run, "
            "outcome, error, member_ids, applied_at) VALUES (%s, %s, 1, false, 'skipped', "
            f"'carries_out_of_scope_listings', %s, {when_sql})",
            (run, gen, members),
        )

    ledger_row("now() - interval '2 days'", "rt:old")
    cur.execute(
        "INSERT INTO autodedup.settings (key, value, updated_at) VALUES (%s, '\"w5\"', now() - interval '1 day')",
        (f"rt_seed_version:{gen}",),
    )
    ledger_row("now() - interval '1 hour'", "rt:new")

    def history() -> list[str]:
        cur.execute(apply_sql.RC_OUTCOME_HISTORY_SQL,
                    {"generation": gen, "listing_ids": members, "depth": 5})
        return [row[1] + "@" + ("new" if row[3] > _one_day_ago(cur) else "old") for row in cur.fetchall()]

    assert history() == ["skipped@new"]
    cur.execute("DELETE FROM autodedup.settings WHERE key = %s", (f"rt_seed_version:{gen}",))
    assert history() == ["skipped@new", "skipped@old"]


def _one_day_ago(cur: Any):
    cur.execute("SELECT now() - interval '1 day'")
    return cur.fetchone()[0]
