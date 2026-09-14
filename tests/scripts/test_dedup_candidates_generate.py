"""The path C generation lane, offline: chunking, the resume cursor, the partial-run guard on
the stale sweep, the failure path, the oracle-vs-SQL comparison, and the three reports — over
a scripted fake connection that answers each statement by what it is (the SQL text), so the
test asserts the lane's control flow and parameters, never the database."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from location_data import location_steps as ls
from scripts import dedup_candidates_generate as lane
from toolkit import dedup_candidates as dc
from toolkit import dedup_candidates_sql as sql
from toolkit import dedup_sim_settings as dss

INPUTS = dc.path_inputs("C", dss.effective_settings(None))


class _Col:
    def __init__(self, name: str) -> None:
        self.name = name


class _Cursor:
    def __init__(self, conn: "_Conn") -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []
        self.description: list[_Col] | None = None
        self.rowcount = -1

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, statement: str, params: Any = None) -> None:
        self.conn.calls.append((statement, params))
        _Conn._require_named_params(statement, params)
        self.rows, cols, self.rowcount = self.conn.answer(statement, params)
        self.description = [_Col(c) for c in cols] if cols else None

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)


class _Tx:
    def __enter__(self) -> "_Tx":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class _Conn:
    """Answers by statement identity. `blocks` = {obec_kod: [listing ids]}."""

    def __init__(self, blocks: dict[str, list[int]], *, running: dict[str, Any] | None = None,
                 fail_on_block: str | None = None, running_status: str = "running") -> None:
        self.blocks = blocks
        self.running = running
        self.running_status = running_status
        self.fail_on_block = fail_on_block
        self.calls: list[tuple[str, Any]] = []
        self.next_id = 100

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def transaction(self) -> _Tx:
        return _Tx()

    def _block(self, p: Any) -> list[int]:
        # the lane passes obec_kod as an int (bigint on the projection); the fake keys by string
        assert isinstance(p["block_key"], int), "block_key must reach SQL as an int"
        return self.blocks[str(p["block_key"])]

    @staticmethod
    def _require_named_params(statement: str, p: Any) -> None:
        """psycopg raises on a named placeholder the caller did not supply; the fake must too,
        or a statement that grows a parameter passes here and fails on the real database."""
        missing = set(re.findall(r"%\((\w+)\)s", statement)) - set(p or {})
        assert not missing, f"query parameter missing: {sorted(missing)}"

    def answer(self, s: str, p: Any) -> tuple[list[tuple[Any, ...]], list[str] | None, int]:
        if s.startswith("SET LOCAL"):
            return [], None, -1
        if "FROM dedup_sim.settings" in s:
            return [], None, -1
        if s == sql.BLOCKS_SQL:
            return [(k, len(v)) for k, v in sorted(self.blocks.items())], None, -1
        if s == sql.BLOCK_IDS_SQL:
            return [(i,) for i in sorted(self._block(p))], None, -1
        if s.startswith("INSERT INTO dedup_sim.simulation_runs"):
            return [(1,)], None, 1
        if s.startswith("INSERT INTO dedup_sim.candidate_inputs"):
            return [(7,)], None, 1
        if s.startswith("INSERT INTO dedup_sim.candidate_generations"):
            return [(42,)], None, 1
        if "FROM dedup_sim.candidate_generations g" in s:
            if self.running is None or p["status"] != self.running_status:
                return [], None, -1
            return [(42, 1, 7, "C", dc.fingerprint(INPUTS), json.dumps(INPUTS), self.running_status,
                     json.dumps(self.running), None)], None, -1
        if s.startswith("UPDATE dedup_sim.candidate_generations SET status = 'running'"):
            return [], None, 1
        if s.startswith("UPDATE dedup_sim.simulation_runs SET status = 'running'"):
            return [], None, 1
        if s.startswith("UPDATE dedup_sim.candidate_generations SET progress"):
            return [], None, 1
        if s.startswith("UPDATE dedup_sim.candidate_generations SET status") or s.startswith("UPDATE dedup_sim.simulation_runs"):
            return [], None, 1
        for rung, stmts in sql.RUNG_SQL.items():
            if s == stmts["insert"]:
                if self.fail_on_block and str(p["block_key"]) == self.fail_on_block:
                    raise RuntimeError("boom on " + str(p["block_key"]))
                ids = [i for i in self._block(p) if p["id_from"] <= i < p["id_to"]]
                return [], None, len(ids)  # one "pair" per lo-side id, per rung
            if s == stmts["count"]:
                ids = [i for i in self._block(p) if p["id_from"] <= i < p["id_to"]]
                return [(len(ids),)], None, -1
        if s == sql.STALE_SWEEP_SQL:
            return [], None, 3
        if s == sql.PAIRS_PER_BLOCK_SQL:
            return [(k, "C1", len(v)) for k, v in self.blocks.items()], ["block_key", "rung", "pairs"], -1
        if s == sql.PAIR_MATRIX_SQL:
            return [("C1", "byt", "byt", "prodej", 5, 4), ("C3", "dum", "dum", "prodej", 2, 0)], \
                ["rung", "category_main_lo", "category_main_hi", "category_type", "pairs", "floor_checked"], -1
        if s == sql.LISTINGS_WITH_CANDIDATES_SQL:
            return [("byt", 6), ("dum", 3)], ["category_main", "listings"], -1
        if s == sql.FUNNEL_SQL:
            # W16: the SHARED step columns. 10 listings -> 9 judged -> 8 located, of which
            # 7 are in a named town, 1 is abroad (an ANSWER) and 0 are a point with no town.
            cols = ["source", "category_main", "category_type", "listings", "with_verdict",
                    "located", "located_town", "located_foreign", "located_no_town", "active",
                    "with_disposition", "with_area", "byt", "byt_with_floor", "c1_eligible",
                    "c3_eligible", "town_no_attribute"]
            return [("sreality", "byt", "prodej", 10, 9, 8, 7, 1, 0, 5, 7, 8, 10, 6, 6, 1, 0)], cols, -1
        if s == sql.TOP_BUCKETS_SQL:
            return [("554782", "Praha", "2+kk", 3, 2)], ["obec_kod", "obec_name", "disposition", "listings", "active"], -1
        if s == sql.BLOCK_NAMES_SQL:
            assert all(isinstance(k, int) for k in p["keys"])
            return [(str(k), "Town " + str(k)) for k in p["keys"]], ["obec_kod", "obec_name"], -1
        raise AssertionError("unexpected statement: " + s[:80])

    def statements(self, prefix_or_exact: str) -> list[tuple[str, Any]]:
        return [c for c in self.calls if c[0] == prefix_or_exact or c[0].startswith(prefix_or_exact)]


# ------------------------------------------------------------------ helpers


def test_chunk_ranges_are_half_open_and_the_last_runs_to_id_max() -> None:
    assert lane.chunk_ranges([], 2) == []
    assert lane.chunk_ranges([5], 2) == [(5, lane.ID_MAX)]
    assert lane.chunk_ranges([1, 2, 3, 4, 5], 2) == [(1, 3), (3, 5), (5, lane.ID_MAX)]
    assert lane.chunk_ranges([1, 2, 3, 4], 2) == [(1, 3), (3, lane.ID_MAX)]


def test_parse_keys_and_scopes() -> None:
    assert lane._parse_keys(" 554782, 582786 ,") == ["554782", "582786"]
    assert lane._parse_keys("") == []
    assert lane._scopes("all,active") == ["all", "active"]
    assert lane._scopes("") == ["all"]
    with pytest.raises(SystemExit):
        lane._scopes("everything")


def test_distribution_bands_towns_by_pair_count() -> None:
    rows = [{"C1": 0, "C3": 0}, {"C1": 5, "C3": 5}, {"C1": 1_000_000, "C3": 1}]
    dist = lane._distribution(rows)
    assert dist[0] == {"pairs_from": 0, "pairs_to": 0, "towns": 1}
    assert dist[1]["towns"] == 1
    assert dist[-1] == {"pairs_from": 1_000_001, "pairs_to": None, "towns": 1}
    assert sum(d["towns"] for d in dist) == 3


# ------------------------------------------------------------------ generate


def test_generate_walks_every_town_in_id_chunks_and_records_progress() -> None:
    conn = _Conn({"500001": [1, 2, 3, 4, 5], "500002": [9]})
    result = lane.generate(conn, "C", only=(), chunk=2, resume=False, top=5, dry_run=False)
    assert result["generation_id"] == 42 and result["simulation_run_id"] == 1
    inserts = [c[1] for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["insert"]]
    assert [(p["block_key"], p["id_from"], p["id_to"]) for p in inserts] == [
        (500001, 1, 3), (500001, 3, 5), (500001, 5, lane.ID_MAX), (500002, 9, lane.ID_MAX),
    ]
    assert all(p["inputs_id"] == 7 and p["generation_id"] == 42 for p in inserts)
    assert all(p["floor_tolerance"] == 2 and p["active_only"] is False for p in inserts)
    # C3 ran on exactly the same chunks
    c3 = [c[1] for c in conn.calls if c[0] == sql.RUNG_SQL["C3"]["insert"]]
    assert [(p["block_key"], p["id_from"], p["id_to"]) for p in c3] == [(p["block_key"], p["id_from"], p["id_to"]) for p in inserts]
    # progress after every chunk and after every block, cursor = last chunk's id_to
    progress = [json.loads(c[1]["progress"]) for c in conn.statements("UPDATE dedup_sim.candidate_generations SET progress")]
    assert progress[0]["last_block_key"] == "500001" and progress[0]["last_id_to"] == 3
    assert progress[-1]["blocks_done"] == 2 and progress[-1]["last_id_to"] == lane.ID_MAX
    assert progress[-1]["chunks_done"] == 4
    assert progress[-1]["pairs_upserted"] == 2 * (5 + 1)  # fake: one row per lo id per rung
    # a complete run sweeps stale rows and closes both rows as success
    assert conn.statements(sql.STALE_SWEEP_SQL)[0][1] == {"inputs_id": 7, "generation_id": 42}
    finish = conn.statements("UPDATE dedup_sim.candidate_generations SET status")[0][1]
    assert finish["status"] == "success"
    stats = json.loads(finish["stats"])
    assert stats["pairs"] == {"C1": 5, "C3": 2, "total": 7}
    assert stats["stale_deleted"] == 3 and stats["partial"] is False
    assert stats["listings_with_candidates"] == [{"category_main": "byt", "listings": 6}, {"category_main": "dum", "listings": 3}]
    assert stats["top_towns"][0]["obec_name"] == "Town 500001"
    assert stats["scope"] == "all"
    # W16: the run stamps the SHARED chain, with the lane's arithmetic already done.
    assert [r["step_key"] for r in stats["waterfall"]] == list(ls.RUN_STEPS)
    assert stats["computed_at"].endswith("Z")
    assert result["pairs"]["total"] == 7


# --------------------------------------------------------- the stamped chain (W16)

# Two portals, the way FUNNEL_SQL returns them: one row per (portal, type, deal).
# 800 listings -> 700 judged -> 630 with a location, of which 600 are in a named town,
# 23 are ABROAD (an answer) and 7 are a Czech point with no town.
_FUNNEL = [
    {"source": "sreality", "listings": 600, "with_verdict": 540, "located": 500,
     "located_town": 480, "located_foreign": 15, "located_no_town": 5,
     "c1_eligible": 420, "c3_eligible": 36},
    {"source": "bazos", "listings": 200, "with_verdict": 160, "located": 130,
     "located_town": 120, "located_foreign": 8, "located_no_town": 2,
     "c1_eligible": 0, "c3_eligible": 100},
]
_PAIRED = [{"category_main": "byt", "listings": 180}, {"category_main": "dum", "listings": 30}]


def _chain(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {r["step_key"]: r for r in rows}


def test_the_stamped_chain_obeys_the_audit_pages_four_laws() -> None:
    """The candidates funnel adopted the audit waterfall's chain, so it must obey the
    same arithmetic the migration proves at apply time — and obey it HERE, once, because
    the browser is forbidden from summing or subtracting anything."""
    rows = lane.waterfall_rows(_FUNNEL, _PAIRED, partial=False)
    by = _chain(rows)
    assert by["all_listings"]["n"] == 800 and by["with_verdict"]["n"] == 700
    assert by["located"]["n"] == 630 and by["located_town"]["n"] == 600
    assert by["eligible"]["n"] == 556 and by["paired"]["n"] == 210

    # Law 1: `lost` is the previous CHAIN row's n minus this one's, and the first is 0.
    previous = None
    for r in rows:
        if r["kind"] != "chain":
            continue
        assert r["lost"] == (0 if previous is None else previous - r["n"]), r["step_key"]
        previous = r["n"]

    # Law 2: a split partitions its parent and carries no loss.
    for parent in ("located",):
        splits = [r for r in rows if r["parent_key"] == parent]
        assert splits and all(r["lost"] is None for r in splits)
        assert sum(r["n"] for r in splits) + by["located_town"]["n"] == by[parent]["n"]

    # Law 3: the share is out of every listing the run looked at.
    for r in rows:
        assert r["share_pct"] == round(r["n"] * 100 / 800, 3), r["step_key"]

    # And the ruling in one line: the town step's loss is exactly abroad plus the Czech
    # point with no town — never one opaque "lost 99,889".
    assert by["located_town"]["lost"] == by["located_foreign"]["n"] + by["located_no_town"]["n"]


def test_the_stamped_chain_leaves_a_partial_runs_last_drop_blank() -> None:
    """A block-limited run pairs its own towns while every step above counts the whole
    corpus, so the drop into `paired` is not a fact about the data. The lane writes NULL
    and the page says why in its banner — never a misleading subtraction."""
    by = _chain(lane.waterfall_rows(_FUNNEL, _PAIRED, partial=True))
    assert by["paired"]["lost"] is None
    assert by["eligible"]["lost"] == 44          # every step above it is still honest


def test_estimate_mode_stamps_the_chain_without_a_paired_row() -> None:
    """Estimate mode writes no pair row, so the chain simply ends at `eligible`: a gap,
    never a zero."""
    rows = lane.waterfall_rows(_FUNNEL, None, partial=False)
    assert "paired" not in _chain(rows)
    assert rows[-1]["step_key"] == "eligible"


def test_the_chain_survives_a_run_that_counted_nothing() -> None:
    """RED by: a division by zero on an empty scope — the share is 0, not a crash."""
    rows = lane.waterfall_rows([], [], partial=False)
    assert all(r["share_pct"] == 0 for r in rows)


def test_partial_run_never_sweeps_stale_rows() -> None:
    conn = _Conn({"500001": [1, 2], "500002": [9]})
    result = lane.generate(conn, "C", only=("500002",), chunk=10, resume=False, top=5, dry_run=False)
    inserts = [c[1]["block_key"] for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["insert"]]
    assert inserts == [500002]
    assert conn.statements(sql.STALE_SWEEP_SQL) == []
    assert result["partial"] is True and result["stale_deleted"] == 0


def test_resume_skips_finished_towns_and_continues_after_the_cursor() -> None:
    running = {"blocks_total": 3, "blocks_done": 1, "chunks_done": 2, "pairs_upserted": 4,
               "last_block_key": "500002", "last_id_to": 12, "partial": False, "only": [], "chunk": 2}
    conn = _Conn({"500001": [1, 2], "500002": [10, 11, 12, 13, 14], "500003": [20]}, running=running)
    lane.generate(conn, "C", only=(), chunk=2, resume=True, top=5, dry_run=False)
    # no new run/inputs/generation rows
    assert conn.statements("INSERT INTO dedup_sim.simulation_runs") == []
    assert conn.statements("INSERT INTO dedup_sim.candidate_generations") == []
    inserts = [c[1] for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["insert"]]
    assert [(p["block_key"], p["id_from"], p["id_to"]) for p in inserts] == [
        (500002, 12, 14), (500002, 14, lane.ID_MAX), (500003, 20, lane.ID_MAX),
    ]
    finish = conn.statements("UPDATE dedup_sim.candidate_generations SET status")[0][1]
    assert finish["status"] == "success"
    assert json.loads(finish["progress"])["blocks_done"] == 3
    assert json.loads(finish["progress"])["chunks_done"] == 5


def test_resume_with_a_finished_last_block_moves_on() -> None:
    running = {"last_block_key": "500001", "last_id_to": lane.ID_MAX, "blocks_done": 1, "chunks_done": 1,
               "pairs_upserted": 1}
    conn = _Conn({"500001": [1, 2], "500002": [9]}, running=running)
    lane.generate(conn, "C", only=(), chunk=10, resume=True, top=5, dry_run=False)
    inserts = [c[1]["block_key"] for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["insert"]]
    assert inserts == [500002]


def test_resumed_pilot_keeps_its_scope_and_never_sweeps() -> None:
    # the interrupted run was a pilot over one town; a resume WITHOUT --blocks must not widen
    # it into a corpus run — the stale sweep would delete every other town's rows
    running = {"partial": True, "only": ["500002"], "last_block_key": "500002", "last_id_to": 11,
               "blocks_done": 0, "chunks_done": 1, "pairs_upserted": 1}
    conn = _Conn({"500001": [1, 2], "500002": [10, 11, 12], "500003": [20]}, running=running)
    result = lane.generate(conn, "C", only=(), chunk=10, resume=True, top=5, dry_run=False)
    inserts = [(c[1]["block_key"], c[1]["id_from"]) for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["insert"]]
    assert inserts == [(500002, 11)]
    assert conn.statements(sql.STALE_SWEEP_SQL) == []
    assert result["partial"] is True and result["only"] == ["500002"]


def test_resume_with_different_blocks_is_refused() -> None:
    running = {"partial": True, "only": ["500002"], "last_block_key": "500002", "last_id_to": 11}
    conn = _Conn({"500001": [1, 2], "500002": [10, 11, 12]}, running=running)
    with pytest.raises(SystemExit, match="opened over blocks"):
        lane.generate(conn, "C", only=("500001",), chunk=10, resume=True, top=5, dry_run=False)
    assert conn.statements("INSERT INTO") == []


def test_a_failed_generation_resumes_and_is_reopened() -> None:
    running = {"partial": False, "only": [], "last_block_key": "500001", "last_id_to": lane.ID_MAX,
               "blocks_done": 1, "chunks_done": 1, "pairs_upserted": 2}
    conn = _Conn({"500001": [1, 2], "500002": [9]}, running=running, running_status="failed")
    lane.generate(conn, "C", only=(), chunk=10, resume=True, top=5, dry_run=False)
    assert len(conn.statements("UPDATE dedup_sim.candidate_generations SET status = 'running'")) == 1
    inserts = [c[1]["block_key"] for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["insert"]]
    assert inserts == [500002]
    assert len(conn.statements(sql.STALE_SWEEP_SQL)) == 1  # a complete corpus run: sweep allowed


def test_resume_without_a_running_generation_opens_a_new_one() -> None:
    conn = _Conn({"500001": [1]}, running=None)
    lane.generate(conn, "C", only=(), chunk=10, resume=True, top=5, dry_run=False)
    assert len(conn.statements("INSERT INTO dedup_sim.candidate_generations")) == 1


def test_failure_records_the_error_and_progress_then_reraises() -> None:
    conn = _Conn({"500001": [1, 2], "500002": [9]}, fail_on_block="500002")
    with pytest.raises(RuntimeError, match="boom on 500002"):
        lane.generate(conn, "C", only=(), chunk=10, resume=False, top=5, dry_run=False)
    finish = conn.statements("UPDATE dedup_sim.candidate_generations SET status")[0][1]
    assert finish["status"] == "failed" and "boom" in finish["error"]
    assert json.loads(finish["progress"])["last_block_key"] == "500001"
    assert conn.statements(sql.STALE_SWEEP_SQL) == []


def test_dry_run_plans_without_opening_a_run() -> None:
    conn = _Conn({"500001": [1, 2, 3], "500002": [9]})
    plan = lane.generate(conn, "C", only=(), chunk=2, resume=False, top=5, dry_run=True)
    assert plan["dry_run"] is True and plan["towns"] == 2 and plan["listings"] == 4
    assert plan["estimated_chunks"] == 3 and plan["fingerprint"] == dc.fingerprint(INPUTS)
    assert conn.statements("INSERT INTO") == []
    assert "DRY RUN" in lane.generate_markdown(plan)


# ------------------------------------------------------------------ estimate


def test_estimate_counts_every_town_for_every_scope_and_writes_nothing() -> None:
    conn = _Conn({"500001": [1, 2, 3], "500002": [9]})
    report = lane.estimate(conn, INPUTS, scopes=["all", "active"], only=(), top=5)
    assert conn.statements("INSERT INTO") == [] and conn.statements("UPDATE") == []
    counts = [c[1] for c in conn.calls if c[0] == sql.RUNG_SQL["C1"]["count"]]
    assert [(p["block_key"], p["active_only"]) for p in counts] == [
        (500001, False), (500002, False), (500001, True), (500002, True)]
    assert all(p["id_from"] == 0 and p["id_to"] == lane.ID_MAX for p in counts)
    s = report["scopes"]["all"]
    assert s["towns"] == 2 and s["listings"] == 4
    assert s["pairs"] == {"C1": 4, "C3": 4, "total": 8}
    assert s["top_towns"][0]["block_key"] == "500001" and s["top_towns"][0]["obec_name"] == "Town 500001"
    assert s["funnel"][0]["source"] == "sreality" and s["top_buckets"][0]["obec_name"] == "Praha"
    # W2-b deleted the town-assignment readout: it keyed on `admin_assignment_method`,
    # which the answer table does not carry.
    assert "town_assignment" not in report
    md = lane.estimate_markdown(report)
    assert "| all |" in md and "| active |" in md and "Town 500001" in md


# ------------------------------------------------------------------ verify


def _attrs(listing_id: int, dispo: str | None, area: float | None = 60.0, floor: int | None = 2) -> dc.ListingAttrs:
    return dc.ListingAttrs(listing_id, "500001", "prodej", "byt", dispo, floor, area, None)


def test_oracle_pairs_is_the_rule_over_every_pair_of_a_town() -> None:
    attrs = [_attrs(1, "2+kk"), _attrs(2, "2+kk"), _attrs(3, "3+kk"), _attrs(4, None), _attrs(5, None, area=62)]
    got = lane.oracle_pairs(attrs, INPUTS)
    assert set(got) == {(1, 2), (1, 4), (2, 4), (3, 4), (1, 5), (2, 5), (3, 5), (4, 5)}
    assert got[(1, 2)].rung == "C1" and got[(1, 4)].rung == "C3" and got[(4, 5)].rung == "C3"


def test_compare_block_reports_every_kind_of_disagreement() -> None:
    oracle = {
        (1, 2): dc.PairVerdict("C1", "2+kk", None, None, None, True),
        (1, 4): dc.PairVerdict("C3", None, 60.0, 60.0, 0.0, True),
        (2, 4): dc.PairVerdict("C3", None, 60.0, 60.0, 0.0, True),
    }
    got = {
        (1, 2): {"rung": "C1", "disposition": "2+kk", "floor_checked": None},  # NULL = mismatch
        (1, 4): {"rung": "C1", "disposition": None, "floor_checked": True},  # rung mismatch
        (3, 4): {"rung": "C3", "floor_checked": False, "area_lo": 1, "area_hi": 1, "area_diff_pct": 0},  # only sql
    }
    cmp = lane.compare_block(oracle, got)
    assert cmp["agree"] is False
    assert cmp["only_oracle"] == [(2, 4)] and cmp["only_sql"] == [(3, 4)]
    assert cmp["rung_mismatch"] == [((1, 4), "C3", "C1")]
    assert cmp["evidence_mismatch"] == [((1, 2), "floor_checked", True, None)]
    assert lane.compare_block(oracle, {k: {"rung": v.rung, "disposition": v.disposition, "floor_checked": v.floor_checked,
                                           "area_lo": v.area_lo, "area_hi": v.area_hi, "area_diff_pct": v.area_diff_pct}
                                       for k, v in oracle.items()})["agree"] is True


def test_verify_markdown_states_the_verdict() -> None:
    result = {"path": "C", "inputs": INPUTS, "agree": False, "blocks": [
        {"block_key": "500001", "listings": 3, "oracle_pairs": 2, "sql_pairs": 1, "only_oracle": [(1, 2)],
         "only_oracle_n": 1, "only_sql": [], "only_sql_n": 0, "rung_mismatch": [], "rung_mismatch_n": 0,
         "evidence_mismatch": [], "evidence_mismatch_n": 0, "agree": False},
        {"block_key": "554782", "listings": 108552, "skipped": "> 3000 listings"},
    ]}
    md = lane.verify_markdown(result)
    assert "**DISAGREE**" in md and "skipped: > 3000 listings" in md and "(1, 2)" in md


def test_verify_requires_blocks() -> None:
    with pytest.raises(SystemExit):
        lane.verify(_Conn({}), INPUTS, only=(), max_listings=10)
