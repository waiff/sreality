"""`probes set=readiness_v1`: registered, read-only by inspection and by the database, one artifact."""

from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from autodedup import census, cohort, lane
from autodedup import readiness_sql as R

_WRITE_TOKENS = (
    "insert", "update", "delete", "merge", "upsert", "create", "alter", "drop", "truncate",
    "grant", "revoke", "copy", "vacuum", "analyze", "refresh", "cluster", "reindex", "lock",
    "call", "do", "into", "nextval", "setval", "set_config", "pg_advisory_lock",
    "pg_terminate_backend", "pg_cancel_backend",
)


def _code_tokens(sql: str) -> set[str]:
    """The SQL's words with string literals and comments removed (a literal like 'merge' is data)."""
    stripped = re.sub(r"'(?:[^']|'')*'", " ", sql)
    stripped = re.sub(r"--[^\n]*", " ", stripped)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL)
    return set(re.findall(r"[a-z_][a-z0-9_]*", stripped.lower()))


def test_readiness_v1_is_a_registered_probe_set() -> None:
    assert R.SET_NAME == "readiness_v1"
    registered = census.PROBE_SETS["readiness_v1"]
    assert [p.name for p in registered] == [q.name for q in R.READINESS_V1]
    assert [p.sql for p in registered] == [q.sql for q in R.READINESS_V1]
    assert census.parse_probes_args({"set": "readiness_v1"})["set"] == "readiness_v1"
    assert census.PROBE_SETS["corpus"] is census.CORPUS_PROBES
    assert lane.MODES["probes"] is census.run_probes


def test_args_select_a_set_and_never_carry_sql() -> None:
    for bad in ({"set": "readiness_v2"}, {"sql": "select 1"}, {"query": "r0_preconditions"}):
        with pytest.raises(ValueError):
            census.parse_probes_args(bad)


def test_every_query_has_a_unique_name_statement_why_and_decision() -> None:
    names = [q.name for q in R.READINESS_V1]
    assert len(set(names)) == len(names) == 29
    assert len({q.sql for q in R.READINESS_V1}) == len(names)
    for q in R.READINESS_V1:
        assert q.why.strip() and q.drives.strip(), q.name


@pytest.mark.parametrize("query", R.READINESS_V1, ids=lambda q: q.name)
def test_every_query_is_read_only_by_inspection(query: R.ReadinessQuery) -> None:
    first = re.match(r"\s*(\w+)", query.sql)
    assert first and first.group(1).lower() in {"select", "with"}, query.name
    forbidden = _code_tokens(query.sql) & set(_WRITE_TOKENS)
    assert not forbidden, f"{query.name} carries write/DDL token(s) {sorted(forbidden)}"
    assert ";" not in query.sql, "one statement per query"
    assert "%" not in query.sql, "no psycopg placeholders or literal percents"


def test_the_inspection_would_catch_a_write() -> None:
    for sql in (
        "with x as (delete from t returning 1) select 1",
        "select 1 into t2 from t",
        "SELECT * FROM t FOR UPDATE",
        "select pg_advisory_lock(1)",
    ):
        assert _code_tokens(sql) & set(_WRITE_TOKENS), sql
    assert not _code_tokens("select 1 where x = 'merge' and y = 'update'") & set(_WRITE_TOKENS)


def test_every_query_is_a_constant_the_sql_prepare_gate_discovers() -> None:
    from tests.sql_corpus import discover

    ours = {
        " ".join(item.sql.split())
        for item in discover(include_inline=False)
        if item.origin.startswith("autodedup/readiness_sql.py:")
    }
    for q in R.READINESS_V1:
        assert " ".join(q.sql.split()) in ours, q.name


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def test_the_engine_merge_review_list_explains_the_field_disagreement_count() -> None:
    (q,) = [q for q in R.READINESS_V1 if q.name == "a_engine_merge_disagreement_list"]
    assert q.sql is R.A_ENGINE_MERGE_DISAGREEMENT_LIST_SQL
    assert q.drives.startswith("Decision 18 / trial week: the operator's review list")
    sql, probe = _flat(q.sql), _flat(R.A_FIELD_DISAGREEMENT_SQL)
    for shared in (
        "from public.listings l",
        "where l.is_active and l.property_id is not null group by l.property_id "
        "having count(*) > 1",
        "min(l.area_m2) as area_min, max(l.area_m2) as area_max",
        "count(distinct l.disposition) as",
        "count(distinct l.floor) as",
        "pr.area_max > pr.area_min * 1.05",
        "join public.properties p on p.id = ",
        "and p.status = 'active'",
    ):
        assert shared in probe and shared in sql, shared
    assert "count(*) filter (where pr.floors > 1) as floor_differs" in probe
    assert "count(*) filter (where pr.dispositions > 1) as disposition_differs" in probe
    assert ("where pr.n_floors > 1 or pr.area_max > pr.area_min * 1.05 "
            "or pr.n_dispositions > 1") in sql
    for column in ("pr.n_floors > 1 as floor_differs",
                   "coalesce(pr.area_max > pr.area_min * 1.05, false) as area_differs_over_5pct",
                   "pr.n_dispositions > 1 as dispo_differs"):
        assert column in sql, column


def test_the_engine_merge_review_list_reads_live_engine_merges_newest_first() -> None:
    sql = _flat(R.A_ENGINE_MERGE_DISAGREEMENT_LIST_SQL)
    migrations = Path(__file__).resolve().parents[2] / "migrations"
    ledger = _flat(next(migrations.glob("558_*.sql")).read_text(encoding="utf-8"))
    assert ("check (outcome in ('planned', 'applied', 'skipped', 'refused', 'failed'))"
            in ledger)
    assert "from autodedup.applied_merges a where a.outcome = 'applied' and a.undone_at is null" \
        in sql
    assert "a.survivor_property_id as property_id, a.generation, a.applied_at as merged_at" in sql
    select = sql.rsplit(" from merged m ", 1)[0].rsplit(") select ", 1)[1]
    depth, parts, current = 0, [], ""
    for ch in select:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
    parts.append(current.strip())
    assert [part.rsplit(" ", 1)[-1].split(".")[-1] for part in parts] == [
        "property_id", "generation", "merged_at", "n_active_adverts", "floors", "areas",
        "dispositions", "sources", "listing_ids", "floor_differs", "area_differs_over_5pct",
        "dispo_differs",
    ]
    assert sql.endswith("order by m.merged_at desc, m.property_id desc limit 200")


def test_trial_blocks_use_the_export_spelling_and_every_area_query_uses_them() -> None:
    blocks = cohort.parse_blocks(" ".join(R.TRIAL_BLOCKS))
    assert [(b.grain, b.code) for b in blocks] == [
        ("town", 563510), ("town", 577626), ("quarter", 490245),
    ]
    towns = ", ".join(str(b.code) for b in blocks if b.grain == "town")
    quarters = [b.code for b in blocks if b.grain == "quarter"]
    predicate = f"ll.obec_kod in ({towns}) or ll.cast_obce_kod = {quarters[0]}"
    area_aware = [q for q in R.READINESS_V1 if "listing_location" in q.sql]
    assert len(area_aware) == 9
    for q in area_aware:
        assert predicate in q.sql, q.name


class _Tx:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self) -> "_Tx":
        self.log.append("BEGIN")
        return self

    def __exit__(self, *exc: object) -> bool:
        self.log.append("END")
        return False


def _conn(answer: Any, log: list[str]) -> Any:
    class Cur:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def execute(self, sql: str, params: Any = None) -> None:
            log.append(sql)
            self.rows = [] if sql.startswith("SET ") else answer(sql)

        @property
        def description(self) -> list[tuple[str]]:
            return [(k,) for k in (self.rows[0] if self.rows else {})]

        def fetchall(self) -> list[tuple[Any, ...]]:
            return [tuple(r.values()) for r in self.rows]

        def __enter__(self) -> "Cur":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

    class Conn:
        def cursor(self) -> Cur:
            return Cur()

        def transaction(self) -> _Tx:
            return _Tx(log)

        def close(self) -> None:
            log.append("CLOSE")

    return Conn


_CANNED: dict[str, list[dict[str, Any]]] = {
    R.R0_PRECONDITIONS_SQL: [
        {"kind": "relation", "name": "autodedup.applied_merges", "value": "ABSENT"},
        {"kind": "setting", "name": "autodedup_apply_enabled", "value": "false"},
    ],
    R.R1_MERGE_GROUPS_SQL: [
        {"source": "auto", "generation": "legacy", "state": "live", "trial_area": "inside",
         "groups": 12},
        {"source": "auto", "generation": "legacy", "state": "live", "trial_area": "straddles",
         "groups": 2},
        {"source": "operator", "generation": "legacy", "state": "live", "trial_area": "inside",
         "groups": 5},
    ],
    R.R1C_LIVE_GROUP_HEALTH_SQL: [
        {"source": "auto", "touches_trial": True, "health": "intact", "groups": 9},
        {"source": "auto", "touches_trial": True, "health": "children_moved", "groups": 3},
    ],
    R.R7_TRIAL_EXPECTED_MERGES_SQL: [
        {"generation": "g12", "status": "proposed", "cross_block": False, "groups": 40,
         "would_merge": 30},
        {"generation": "g12", "status": "proposed", "cross_block": True, "groups": 4,
         "would_merge": 3},
    ],
    R.A_FALSE_ALERT_RATE_SQL: [
        {"direction": "drop", "steps_7d": 80, "cross_advert_steps_7d": 20},
        {"direction": "rise", "steps_7d": 20, "cross_advert_steps_7d": 5},
    ],
    R.E_ARRIVAL_TO_EVIDENCE_SQL: [
        {"source": "ALL", "clip_p50_s": 8900.0, "clip_p90_s": 16200.0},
    ],
    R.B_TAG_HEAD_MODELS_SQL: [{"version": "v3", "status": "active"}],
    R.A_ENGINE_MERGE_DISAGREEMENT_LIST_SQL: [
        {"property_id": 91, "generation": "g12",
         "merged_at": dt.datetime(2026, 9, 25, 9, 30, tzinfo=dt.timezone.utc),
         "n_active_adverts": 2, "floors": [2, 3], "areas": [Decimal("61.0")],
         "dispositions": ["2+kk"], "sources": ["idnes", "sreality"], "listing_ids": [7, 8],
         "floor_differs": True, "area_differs_over_5pct": False, "dispo_differs": False},
    ],
}


def _answer(sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in _CANNED.get(sql, [])]


def test_every_statement_runs_inside_a_read_only_transaction(tmp_path: Path) -> None:
    for args in ({"set": "readiness_v1"}, {}):
        log: list[str] = []
        census.run_probes(_conn(_answer, log), args, tmp_path)
        transactions: list[list[str]] = []
        for entry in log:
            if entry == "BEGIN":
                transactions.append([])
            elif entry not in ("END", "CLOSE"):
                transactions[-1].append(entry)
        expected = len(census.PROBE_SETS[args.get("set", "corpus")])
        assert len(transactions) == expected
        for statements in transactions:
            assert statements[0] == census.READ_ONLY_GUARD == "SET TRANSACTION READ ONLY"
            assert statements[1].startswith("SET LOCAL statement_timeout")
            assert len(statements) == 3


def test_the_town_probe_inherits_the_guard() -> None:
    from autodedup import town_probe

    assert town_probe._run is census._run


def test_the_artifact_shape(tmp_path: Path) -> None:
    result = census.run_probes(_conn(_answer, []), {"set": "readiness_v1"}, tmp_path)
    path = tmp_path / "probes" / "readiness_v1.json"
    assert result["probes_json"] == str(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    names = [q.name for q in R.READINESS_V1]
    assert payload["set"] == "readiness_v1"
    assert payload["trial_blocks"] == list(R.TRIAL_BLOCKS)
    assert sorted(payload["probes"]) == sorted(names)
    assert sorted(payload["timings"]) == sorted(names)
    assert sorted(payload["queries"]) == sorted(names)
    assert payload["queries"]["r1_merge_groups"]["drives"].startswith("Decision 3")
    assert payload["errors"] == []
    assert payload["probes"]["r1c_live_group_health"][0]["health"] == "intact"
    assert payload["probes"]["neg_contradicted_negatives"] == []
    (review,) = payload["probes"]["a_engine_merge_disagreement_list"]
    assert review["merged_at"] == "2026-09-25 09:30:00+00:00"
    assert review["floors"] == [2, 3] and review["areas"] == ["61.0"]
    assert review["listing_ids"] == [7, 8] and review["floor_differs"] is True

    head = payload["headline"]
    assert head == result["headline"]
    assert head["apply_relations_absent"] == ["autodedup.applied_merges"]
    assert head["legacy_live_groups_inside_trial"] == 12
    assert head["legacy_live_groups_straddling_trial"] == 2
    assert head["legacy_intact_groups_touching_trial"] == 9
    assert head["trial_would_merge_by_generation"] == {"g12": 33}
    assert head["trial_cross_block_groups"] == 4
    assert head["negatives_contradicted_pairs"] == 0
    assert head["false_alert_share_7d"] == 0.25
    assert head["clip_p90_s"] == 16200.0
    assert head["active_tag_head_model"] == "v3"
    assert result["queries"] == result["succeeded"] == 29
    assert not (tmp_path / "probes.json").exists()


def test_a_failed_query_is_recorded_not_raised(tmp_path: Path) -> None:
    def answer(sql: str) -> list[dict[str, Any]]:
        if sql is R.B_LEGACY_CRON_JOBS_SQL:
            raise RuntimeError('relation "cron.job" does not exist')
        return _answer(sql)

    result = census.run_probes(_conn(answer, []), {"set": "readiness_v1"}, tmp_path)
    payload = json.loads((tmp_path / "probes" / "readiness_v1.json").read_text(encoding="utf-8"))
    assert "b_legacy_cron_jobs" not in payload["probes"]
    assert any(err.startswith("b_legacy_cron_jobs:") for err in payload["errors"])
    assert payload["headline"]["legacy_cron_jobs"] is None
    assert payload["headline"]["legacy_live_groups_inside_trial"] == 12
    assert result["succeeded"] == 28


def test_the_lane_dispatch_writes_the_artifact_and_a_summary(tmp_path: Path) -> None:
    code = lane.run(
        "probes", "set=readiness_v1,timeout_s=300", tmp_path, conn_factory=_conn(_answer, [])
    )
    assert code == 0
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["result"]["set"] == "readiness_v1"
    assert summary["result"]["headline"]["legacy_live_groups_inside_trial"] == 12
    assert (tmp_path / "probes" / "readiness_v1.json").exists()
