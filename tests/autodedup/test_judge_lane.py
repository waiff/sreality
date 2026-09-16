"""The judge lane end to end against fakes — no network, no database, no provider, no spend.

Every seam the lane owns is exercised here: the `--max-usd` refusal (E31), the dry run that
builds prompts and calls nothing, the budget that stops LAUNCHING mid-pass while keeping the
verdicts already paid for, the "done, not merely drawn" exit code, the `autodedup.judgements`
column contract of migration 528, sample determinism, and the three gold votes plus their
fallback when the independent model family is unavailable.
"""

from __future__ import annotations

import gzip
import io
import json
import re
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage

from autodedup import harness, judge, judge_lane
from autodedup.judge_sql import (
    JUDGEMENT_CACHED_SQL,
    JUDGEMENT_COST_SQL,
    JUDGEMENT_UPSERT_SQL,
)
from tests.autodedup.test_engine_e2e import build_records

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "528_autodedup_foundation.sql"


# --- fakes -------------------------------------------------------------------------------


def _jpeg() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (24, 18), (120, 140, 160)).save(buf, format="JPEG", quality=70)
    return buf.getvalue()


class FakeR2:
    def __init__(self) -> None:
        self.reads: list[str] = []

    def download_bytes(self, key: str) -> bytes:
        self.reads.append(key)
        return _jpeg()


class FakeResponse:
    def __init__(self, tool_calls: list[dict[str, Any]], cost_usd: float, call_id: int) -> None:
        self.tool_calls = tool_calls
        self.cost_usd = cost_usd
        self.llm_call_id = call_id
        self.text = ""


VERDICT_INPUT: dict[str, Any] = {
    "verdict": "different_property",
    "confidence": 0.82,
    "deal_or_category_conflict": False,
    "unit_discriminator": "floor 2 versus floor 5",
    "key_evidence": ["same street", "same broker"],
    "contradicting_evidence": ["different floor"],
    "developer_project_suspected": True,
}


class FakeLLM:
    """One canned forced-tool verdict per call, with a per-model failure switch."""

    def __init__(self, calls: list[dict[str, Any]], cost: float = 0.001,
                 fail_models: tuple[str, ...] = (), fail_all: bool = False,
                 error: str = "provider says no",
                 transient_fails: dict[str, int] | None = None) -> None:
        self.calls = calls
        self.cost = cost
        self.fail_models = fail_models
        self.fail_all = fail_all
        self.error = error
        self.transient_fails = dict(transient_fails or {})

    def call(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        model = kwargs["model"]
        left = self.transient_fails.get(model, 0)
        if left:
            self.transient_fails[model] = left - 1
            raise RuntimeError("openai call failed: HTTP 429 slow down")
        if self.fail_all or model in self.fail_models:
            raise RuntimeError(self.error)
        return FakeResponse(
            [{"id": "t1", "name": judge.TOOL_NAME, "input": dict(VERDICT_INPUT)}],
            self.cost,
            len(self.calls),
        )


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if sql is JUDGEMENT_UPSERT_SQL and self._conn.upserts_fail:
            raise RuntimeError('relation "autodedup.judgements" does not exist')
        if sql is JUDGEMENT_CACHED_SQL:
            self._rows = list(self._conn.cached)
        elif sql is JUDGEMENT_COST_SQL:
            self._rows = [self._conn.cost_row]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, executed: list[tuple[str, Any]], cached: list[tuple[int, int]],
                 cost_row: tuple[Any, Any], upserts_fail: bool = False) -> None:
        self.executed = executed
        self.cached = cached
        self.cost_row = cost_row
        self.upserts_fail = upserts_fail
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def cohort(tmp_path: Path) -> Path:
    path = tmp_path / "cohort.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in build_records():
            handle.write(json.dumps(record) + "\n")
    return path


@pytest.fixture()
def lane(monkeypatch: pytest.MonkeyPatch, cohort: Path):
    """Return a runner: `lane(tier=..., **args) -> (summary, calls, executed, r2)`."""
    calls: list[dict[str, Any]] = []
    executed: list[tuple[str, Any]] = []
    state: dict[str, Any] = {
        "client": FakeLLM(calls),
        "cached": [],
        "cost_row": (None, 0),
        "r2": FakeR2(),
        "downloads": [],
        "upserts_fail": False,
    }

    def fake_download(export_run: str, dest: Path) -> Path:
        state["downloads"].append((export_run, Path(dest)))
        Path(dest).mkdir(parents=True, exist_ok=True)
        target = Path(dest) / judge_lane.COHORT_FILE
        target.write_bytes(cohort.read_bytes())
        return target

    state["sleeps"] = []
    monkeypatch.setattr(judge_lane, "RETRY_SLEEP", lambda seconds: state["sleeps"].append(seconds))
    monkeypatch.setattr(judge_lane, "download_cohort", fake_download)
    monkeypatch.setattr(judge_lane, "llm_client", lambda conn: state["client"])
    monkeypatch.setattr(judge_lane, "image_store", lambda: state["r2"])

    def factory() -> FakeConn:
        return FakeConn(executed, state["cached"], state["cost_row"],
                        bool(state.get("upserts_fail")))

    def run(out: Path, **args: Any) -> dict[str, Any]:
        raw = {key: str(value) for key, value in args.items()}
        return judge_lane.run_judge(factory, raw, out)

    run.state = state  # type: ignore[attr-defined]
    run.calls = calls  # type: ignore[attr-defined]
    run.executed = executed  # type: ignore[attr-defined]
    return run


def _upserts(executed: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [params for sql, params in executed if sql is JUDGEMENT_UPSERT_SQL]


def _judgements(out: Path) -> list[dict[str, Any]]:
    path = out / judge_lane.JUDGEMENTS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- the budget contract -------------------------------------------------------------------


def test_the_lane_refuses_to_start_without_max_usd(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="123", tier="text", n=2)
    assert "max_usd" in str(exc.value)
    assert not lane.calls


@pytest.mark.parametrize("bad", [{"max_usd": "0"}, {"max_usd": "nope"}, {"tier": "deep"}])
def test_bad_arguments_are_refused_before_any_work(lane, tmp_path: Path, bad: dict) -> None:
    args = {"export_run": "123", "tier": "text", "n": 2, "max_usd": 5}
    args.update(bad)
    with pytest.raises(SystemExit):
        lane(tmp_path / "out", **args)
    assert not lane.calls


def test_export_run_is_required_and_the_download_is_used(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="4242", tier="text", n=2, max_usd=5, workers=1)
    assert lane.state["downloads"] and lane.state["downloads"][0][0] == "4242"
    assert summary["export_run"] == "4242"


def test_dry_run_builds_prompts_and_calls_nothing(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="vision", n=4, max_usd=5, dry_run=1)
    assert lane.calls == []
    assert summary["spent_usd"] == 0.0
    estimate = summary["estimate"]
    assert estimate["pairs"] == summary["drawn"] > 0
    assert estimate["calls"] == estimate["pairs"]
    assert estimate["images"] > 0
    assert estimate["prompt_chars"] > 0
    assert estimate["est_cost_usd"] == round(
        estimate["calls"] * judge_lane.EST_COST_USD["vision"], 4
    )
    assert estimate["fits_budget"] is True
    assert not _upserts(lane.executed)
    assert lane.state["r2"].reads == []


def test_budget_stop_marks_skipped_and_keeps_the_verdicts_already_paid_for(
    lane, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    # One call fits under the cap; the second look-ahead does not.
    summary = lane(out, export_run="1", tier="text", n=4, max_usd=0.002, workers=1)
    assert summary["attempted"] == 1
    assert summary["done"] == 1
    assert summary["skipped_budget"] == summary["drawn"] - 1 > 0
    assert summary["budget_stopped"] is True
    assert summary["spent_usd"] == pytest.approx(0.001)
    assert len(_upserts(lane.executed)) == 1
    assert len(_judgements(out)) == 1


def test_done_zero_with_attempts_exits_non_zero(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(lane.calls, fail_all=True)
    with pytest.raises(SystemExit) as exc:
        lane(out, export_run="1", tier="text", n=2, max_usd=5, workers=1)
    assert "0 done" in str(exc.value)
    written = json.loads((out / judge_lane.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert written["attempted"] > 0 and written["done"] == 0
    assert written["failed"] == written["attempted"]


def test_the_lane_exits_non_zero_through_the_mode_registry(lane, tmp_path: Path) -> None:
    from autodedup import lane as lane_module

    assert lane_module.MODES["judge"] is judge_lane.run_judge
    assert lane_module.ITERATION_META["judge"]["wave"] == "W3"


# --- persistence ----------------------------------------------------------------------------


def _migration_columns() -> list[str]:
    body = MIGRATION.read_text(encoding="utf-8")
    block = re.search(
        r"create table if not exists autodedup\.judgements \((.*?)\n\);", body, re.S
    )
    assert block
    columns: list[str] = []
    for line in block.group(1).splitlines():
        line = line.strip()
        match = re.match(r"^([a-z_]+)\s+(bigint|text|real|boolean|numeric|timestamptz)", line)
        if match and match.group(1) not in ("primary", "constraint"):
            columns.append(match.group(1))
    return columns


def test_upsert_params_match_migration_528(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    lane(out, export_run="1", tier="text", n=2, max_usd=5, workers=1)
    params = _upserts(lane.executed)
    assert params
    expected = set(_migration_columns()) - {"created_at"}
    assert expected and set(params[0]) == expected
    row = params[0]
    assert row["listing_lo"] < row["listing_hi"]
    assert row["tier"] == "text" and row["model"] == judge_lane.MODEL_TEXT
    assert row["judge_version"] == judge.JUDGE_VERSION
    assert row["verdict"] == "different_property"
    assert row["key_evidence"] == VERDICT_INPUT["key_evidence"]
    assert row["developer_project_suspected"] is True
    assert row["llm_call_id"] and row["cost_usd"] == pytest.approx(0.001)


def test_every_upsert_column_appears_in_the_sql() -> None:
    for column in set(_migration_columns()) - {"created_at"}:
        assert f"%({column})s" in JUDGEMENT_UPSERT_SQL


def test_a_cached_pair_is_never_paid_for_twice(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    first = lane(out, export_run="1", tier="text", n=2, max_usd=5, workers=1)
    pairs = [(row["lo"], row["hi"]) for row in _judgements(out)]
    lane.state["cached"] = pairs
    again = lane(tmp_path / "out2", export_run="1", tier="text", n=2, max_usd=5, workers=1)
    assert again["skipped_cached"] == first["done"]
    assert again["attempted"] == first["attempted"] - first["done"]


def test_cost_is_reconciled_from_llm_calls_when_the_ledger_answers(
    lane, tmp_path: Path
) -> None:
    lane.state["cost_row"] = (0.0425, 3)
    summary = lane(tmp_path / "out", export_run="1", tier="text", n=2, max_usd=5, workers=1)
    assert summary["spent_usd"] == pytest.approx(0.0425)
    assert summary["spent_usd_source"] == "llm_calls"
    assert any(sql is JUDGEMENT_COST_SQL for sql, _ in lane.executed)


# --- the sample -------------------------------------------------------------------------------


def test_the_sample_is_deterministic_by_seed(lane, tmp_path: Path) -> None:
    one = lane(tmp_path / "a", export_run="1", tier="text", n=4, max_usd=5, dry_run=1, seed=7)
    two = lane(tmp_path / "b", export_run="1", tier="text", n=4, max_usd=5, dry_run=1, seed=7)
    pairs_a = [(row["lo"], row["hi"]) for row in json.loads(
        (tmp_path / "a" / judge_lane.SAMPLE_FILE).read_text())["pairs"]]
    pairs_b = [(row["lo"], row["hi"]) for row in json.loads(
        (tmp_path / "b" / judge_lane.SAMPLE_FILE).read_text())["pairs"]]
    assert pairs_a == pairs_b and one["drawn"] == two["drawn"] > 0


def test_text_and_vision_tiers_judge_the_same_sample(lane, tmp_path: Path) -> None:
    lane(tmp_path / "t", export_run="1", tier="text", n=4, max_usd=5, dry_run=1, seed=11)
    lane(tmp_path / "v", export_run="1", tier="vision", n=4, max_usd=5, dry_run=1, seed=11)
    text = json.loads((tmp_path / "t" / judge_lane.SAMPLE_FILE).read_text())
    vision = json.loads((tmp_path / "v" / judge_lane.SAMPLE_FILE).read_text())
    assert [(r["lo"], r["hi"]) for r in text["pairs"]] == [
        (r["lo"], r["hi"]) for r in vision["pairs"]
    ]


def test_the_strata_carry_the_catalogue_only_class(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", tier="text", n=4, max_usd=5, dry_run=1)
    assert harness.CATALOG_ONLY_STRATUM in summary["sample_strata"]
    assert any("|K-" in key for key in summary["sample_strata"])


def test_a_stratum_filter_narrows_the_population(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", tier="text", n=4, max_usd=5,
                   dry_run=1, strata="merge")
    assert summary["sample_strata"]
    assert all(key.startswith("merge") for key in summary["sample_strata"])


# --- tiers --------------------------------------------------------------------------------------


def test_vision_sends_images_read_through_the_store(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="vision", n=2, max_usd=5, workers=1)
    assert summary["calls"] == {"vision": summary["attempted"]}
    assert lane.state["r2"].reads
    first = lane.calls[0]
    assert first["model"] == judge_lane.MODEL_VISION
    assert first["called_for"] == "autodedup_judge_vision"
    assert first["tool_choice"] == judge.TOOL_NAME
    assert first["max_tokens"] == judge_lane.MAX_TOKENS
    content = first["messages"][0]["content"]
    assert sum(1 for block in content if block.get("type") == "image") > 0


def test_smoke_is_ten_pairs_of_text_and_vision(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", tier="smoke", n=400, max_usd=5, workers=1)
    assert summary["drawn"] <= judge_lane.SMOKE_PAIRS
    assert summary["calls"]["text"] == summary["drawn"]
    assert summary["calls"]["vision"] == summary["drawn"]
    tiers = {row["tier"] for row in _judgements(tmp_path / "out")}
    assert tiers == {"text", "vision"}


def test_gold_casts_three_votes_and_stores_one_aggregate(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="gold", n=2, max_usd=5, workers=1)
    pairs = summary["drawn"]
    assert summary["calls"]["gold"] == 3 * pairs
    models = [call["model"] for call in lane.calls[:3]]
    assert models == [judge_lane.MODEL_VISION, judge_lane.MODEL_VISION,
                      judge_lane.MODEL_GOLD_THIRD]
    assert {call["called_for"] for call in lane.calls} == {"autodedup_judge_gold"}
    rows = _judgements(out)
    aggregates = [row for row in rows if row.get("n_votes")]
    assert len(aggregates) == pairs
    assert aggregates[0]["n_votes"] == 3
    assert aggregates[0]["verdict"]["unanimous"] is True
    upserts = _upserts(lane.executed)
    assert len(upserts) == pairs and {row["tier"] for row in upserts} == {"gold"}
    assert upserts[0]["confidence"] == pytest.approx(1.0)


def test_an_unavailable_third_family_falls_back_and_is_flagged_weaker(
    lane, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(lane.calls, fail_models=(judge_lane.MODEL_GOLD_THIRD,))
    summary = lane(out, export_run="1", tier="gold", n=2, max_usd=5, workers=1)
    assert summary["failed"] == summary["drawn"]
    assert summary["done"] == 3 * summary["drawn"]
    aggregates = [row for row in _judgements(out) if row.get("n_votes")]
    assert aggregates and all(row["weaker"] is True for row in aggregates)
    assert all(row["n_votes"] == 3 for row in aggregates)


# --- the summary ------------------------------------------------------------------------------


def test_summary_reports_every_required_field(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="9", tier="text", n=4, max_usd=5, workers=2)
    for key in ("drawn", "attempted", "done", "failed", "skipped_budget", "spent_usd",
                "tier", "judge_version", "seed", "max_usd", "budget_stopped", "engine",
                "sample_strata", "elapsed_s", "mean_cost_usd", "verdicts"):
        assert key in summary, key
    assert summary["done"] == summary["attempted"] == summary["drawn"]
    assert summary["verdicts"] == {"different_property": summary["done"]}
    assert summary["engine"]["pairs_scored"] > 0
    assert (out / judge_lane.RUN_FILE).is_file()
    assert (out / judge_lane.SAMPLE_FILE).is_file()
    assert json.loads((out / judge_lane.SUMMARY_FILE).read_text()) == summary


def test_descriptions_are_rescrubbed_before_they_are_sent() -> None:
    from autodedup.dataset import Listing

    listing = Listing(id=1, block="b", description="Volejte +420 777 123 456 " + "x" * 4000)
    out = judge_lane.scrubbed(listing)
    assert "777 123 456" not in (out.description or "")
    assert len(out.description or "") == judge_lane.DESCRIPTION_MAX


# --- the rails: every way a paid pass can look green while doing nothing -------------------


def test_a_budget_below_one_pair_is_refused_before_the_sample_is_drawn(
    lane, tmp_path: Path
) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1", tier="text", n=4, max_usd=0.0005)
    assert "single text pair" in str(exc.value)
    assert not lane.calls


def test_max_usd_above_the_hard_cap_is_refused(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1", tier="text", n=4, max_usd=50)
    assert "hard per-run cap" in str(exc.value)
    assert not lane.calls


def test_a_drawn_but_unjudged_pass_exits_non_zero() -> None:
    counters = judge_lane.Counters(drawn=17, pairs_processed=17, skipped_budget=17)
    with pytest.raises(SystemExit) as exc:
        judge_lane._rails(counters, judge_lane.Budget(1.0),
                          judge_lane.parse_args({"export_run": "1", "tier": "text",
                                                 "max_usd": "1"}), 0.0)
    assert "0 done" in str(exc.value)


def test_a_pair_that_vanishes_from_the_accounting_exits_non_zero() -> None:
    counters = judge_lane.Counters(drawn=17, pairs_processed=16, done=16)
    with pytest.raises(SystemExit) as exc:
        judge_lane._rails(counters, judge_lane.Budget(1.0),
                          judge_lane.parse_args({"export_run": "1", "tier": "text",
                                                 "max_usd": "1"}), 0.1)
    assert "never judged and never accounted for" in str(exc.value)


def test_a_pair_that_raises_before_the_call_is_counted_not_lost(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "out"
    real = judge_lane._inputs
    seen: list[int] = []

    def exploding(judge_mod, job, la, lb):
        seen.append(job.lo)
        if len(seen) == 1:
            raise ValueError("digest blew up")
        return real(judge_mod, job, la, lb)

    monkeypatch.setattr(judge_lane, "_inputs", exploding)
    summary = lane(out, export_run="1", tier="text", n=6, max_usd=5, workers=1)
    assert summary["pairs_failed"] == 1
    assert summary["pairs_processed"] == summary["drawn"] - 1
    assert summary["done"] == summary["drawn"] - 1 > 0
    assert any("digest blew up" in message for message in summary["errors"])


def test_a_fatal_provider_error_exits_non_zero_and_is_not_called_a_budget_stop(
    lane, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(lane.calls, fail_all=True,
                                   error="openai call failed: HTTP 401 invalid_api_key")
    with pytest.raises(SystemExit) as exc:
        lane(out, export_run="1", tier="text", n=6, max_usd=5, workers=1)
    assert "failed fatally" in str(exc.value)
    written = json.loads((out / judge_lane.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert written["fatal"]
    assert written["skipped_fatal"] == written["drawn"] - 1
    assert written["skipped_budget"] == 0
    assert written["budget_stopped"] is False


def test_a_store_that_takes_nothing_fails_the_lane(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    lane.state["upserts_fail"] = True
    with pytest.raises(SystemExit) as exc:
        lane(out, export_run="1", tier="text", n=4, max_usd=5, workers=1)
    assert "store write(s) failed" in str(exc.value)
    written = json.loads((out / judge_lane.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert written["done"] > 0
    assert written["persist_failed"] == written["persist_attempted"] == written["done"]
    assert written["errors_total"] == written["done"]
    assert len(_judgements(out)) == written["done"]


# --- gold is ground truth: it is never written from a short plan -----------------------------


def test_a_short_gold_plan_writes_no_row_and_is_reported(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(
        lane.calls, fail_models=(judge_lane.MODEL_GOLD_THIRD,),
        error="openai call failed: HTTP 429 slow down",
    )
    summary = lane(out, export_run="1", tier="gold", n=2, max_usd=5, workers=1)
    assert summary["gold_incomplete"] == summary["drawn"]
    assert not _upserts(lane.executed)
    records = [row for row in _judgements(out) if row.get("incomplete")]
    assert records and all(row["n_votes"] == 2 for row in records)


def test_a_transient_failure_on_the_third_family_is_retried_not_downgraded(
    lane, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(
        lane.calls, transient_fails={judge_lane.MODEL_GOLD_THIRD: 1}
    )
    summary = lane(out, export_run="1", tier="gold", n=2, max_usd=5, workers=1)
    assert lane.state["sleeps"] == [judge_lane.RETRY_BACKOFF_S[0]]
    assert summary["failed"] == 0
    assert summary["gold_incomplete"] == 0
    aggregates = [row for row in _judgements(out) if row.get("n_votes")]
    assert aggregates and all(row["weaker"] is False for row in aggregates)
    assert {row["model"] for row in _upserts(lane.executed)} == {
        f"{judge_lane.MODEL_VISION}+{judge_lane.MODEL_VISION}+{judge_lane.MODEL_GOLD_THIRD}"
    }


def test_the_weaker_fallback_is_stamped_into_the_stored_model(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(lane.calls, fail_models=(judge_lane.MODEL_GOLD_THIRD,))
    lane(out, export_run="1", tier="gold", n=2, max_usd=5, workers=1)
    assert all("(weaker)" in row["model"] for row in _upserts(lane.executed))


# --- cost -------------------------------------------------------------------------------------


def test_the_gold_per_call_estimates_sum_to_the_spec_per_pair_price() -> None:
    plan = judge_lane.vote_plan("gold")
    assert len(plan) == judge_lane.GOLD_VOTES
    assert sum(vote.est_usd for vote in plan) == pytest.approx(
        judge_lane.GOLD_PER_PAIR_USD, rel=0.1
    )
    assert judge_lane.min_budget_usd("text") == pytest.approx(
        judge_lane.EST_COST_USD["text"]
    )


def test_a_gold_dry_run_prices_the_pair_not_three_pairs(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", tier="gold", n=2, max_usd=5, dry_run=1)
    estimate = summary["estimate"]
    assert estimate["calls"] == 3 * estimate["pairs"]
    assert estimate["est_cost_per_pair_usd"] == pytest.approx(
        judge_lane.GOLD_PER_PAIR_USD, rel=0.1
    )


def test_a_ledger_read_of_zero_never_reports_a_paid_pass_as_free(
    lane, tmp_path: Path
) -> None:
    lane.state["cost_row"] = (0.0, 0)
    summary = lane(tmp_path / "out", export_run="1", tier="text", n=2, max_usd=5, workers=1)
    assert summary["spent_usd_in_process"] > 0
    assert summary["spent_usd"] == pytest.approx(summary["spent_usd_in_process"])
    assert summary["spent_usd_source"].startswith("in_process (llm_calls summed 0")


def test_every_tier_model_is_priced_by_its_provider() -> None:
    from api.providers.openai import PRICES as OPENAI_PRICES
    from api.providers.qwen import PRICES as QWEN_PRICES

    assert judge_lane.MODEL_TEXT in OPENAI_PRICES
    assert judge_lane.MODEL_VISION in OPENAI_PRICES
    assert judge_lane.MODEL_GOLD_THIRD in QWEN_PRICES


def test_every_called_for_value_is_accepted_by_migration_527() -> None:
    body = (MIGRATION.parent / "527_autodedup_called_for.sql").read_text(encoding="utf-8")
    allowed = set(re.findall(r"'(autodedup_judge_[a-z]+)'", body))
    assert allowed and set(judge_lane.CALLED_FOR.values()) <= allowed


def test_the_ledger_row_carries_the_measured_spend() -> None:
    from autodedup import lane as lane_module

    assert lane_module._spent_usd({"spent_usd": 1.25}) == pytest.approx(1.25)
    assert lane_module._spent_usd({"counts": {}}) is None
    assert lane_module._metrics({"spent_usd": 1.25, "done": 4, "drawn": 5})["done"] == 4


# --- artifacts ---------------------------------------------------------------------------------


def test_the_cache_read_zips_the_two_id_arrays_rather_than_crossing_them() -> None:
    assert "unnest(%(los)s::bigint[], %(his)s::bigint[])" in JUDGEMENT_CACHED_SQL
    assert "listing_lo = any" not in JUDGEMENT_CACHED_SQL


def test_a_second_pass_into_one_out_dir_does_not_double_the_backstop(
    lane, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    first = lane(out, export_run="1", tier="text", n=2, max_usd=5, workers=1)
    again = lane(out, export_run="1", tier="text", n=2, max_usd=5, workers=1)
    assert len(_judgements(out)) == again["done"] == first["done"]


def test_the_sample_reports_how_far_the_floor_pushed_it_past_n(lane, tmp_path: Path) -> None:
    summary = lane(tmp_path / "out", export_run="1", tier="text", n=1, max_usd=5, dry_run=1)
    assert summary["n_selected"] == summary["drawn"]
    assert summary["sample_inflated_by_floor"] == summary["n_selected"] - 1


def test_the_dry_run_estimate_counts_the_system_prompt_and_tool_schema(
    lane, tmp_path: Path
) -> None:
    summary = lane(tmp_path / "out", export_run="1", tier="text", n=2, max_usd=5, dry_run=1)
    estimate = summary["estimate"]
    assert estimate["prompt_chars"] > estimate["calls"] * len(judge.SYSTEM_PROMPT)
    assert estimate["min_usd"] == pytest.approx(judge_lane.EST_COST_USD["text"])


# --- integration pass: what the first end-to-end run against the real cohort exposed ---------


def test_a_reservation_holds_its_estimate_until_the_call_settles() -> None:
    """Six workers cannot all pass the gate on the same stale "nothing spent yet" total."""
    budget = judge_lane.Budget(0.010)
    assert budget.reserve(0.004) is True
    assert budget.reserve(0.004) is True
    # Nothing has been BILLED yet, but $0.008 is in flight: the third call does not fit.
    assert budget.reserve(0.004) is False
    assert budget.stopped is True
    budget.settle(0.004, 0.003)
    budget.settle(0.004, 0.003)
    assert budget.spent == pytest.approx(0.006)
    assert budget.committed == pytest.approx(0.0)


def test_a_released_reservation_returns_its_headroom() -> None:
    budget = judge_lane.Budget(0.010)
    assert budget.reserve(0.009) is True
    budget.release(0.009)
    assert budget.committed == pytest.approx(0.0)
    assert budget.reserve(0.009) is True


def test_the_pass_never_bills_more_than_max_usd(lane, tmp_path: Path) -> None:
    """The first real-cohort dry pass reported $1.0068 against a $1.00 cap."""
    out = tmp_path / "out"
    lane.state["client"] = FakeLLM(lane.calls, cost=0.004)
    summary = lane(out, export_run="1", tier="text", n=8, max_usd=0.012, workers=4)
    assert summary["done"] > 0
    assert summary["spent_usd"] <= summary["max_usd"]


def test_a_billed_response_that_cannot_be_parsed_still_costs_money(
    lane, tmp_path: Path
) -> None:
    """A response that arrived and failed to parse was paid for; dropping its cost hides
    real spend from both the cap and the summary."""

    class Unparseable(FakeLLM):
        def call(self, **kwargs: Any) -> FakeResponse:
            response = super().call(**kwargs)
            response.tool_calls = []
            return response

    out = tmp_path / "out"
    lane.state["client"] = Unparseable(lane.calls, cost=0.001)
    with pytest.raises(SystemExit):
        lane(out, export_run="1", tier="text", n=4, max_usd=5, workers=1)
    written = json.loads((out / judge_lane.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert written["done"] == 0
    assert written["failed"] == written["attempted"] > 0
    assert written["billed_unparsed"] == written["failed"]
    assert written["spent_usd_in_process"] == pytest.approx(0.001 * written["failed"])


def test_a_gold_pair_that_never_got_a_vote_is_not_called_incomplete(
    lane, tmp_path: Path
) -> None:
    """A budget stop leaves hundreds of untouched gold pairs; counting them as `incomplete`
    reports money thrown away that was never spent."""
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="gold", n=8, max_usd=0.020, workers=1)
    assert summary["budget_stopped"] is True
    assert summary["gold_unstarted"] > 0
    assert summary["gold_incomplete"] + summary["gold_unstarted"] <= summary["drawn"]
    unstarted = [row for row in _judgements(out) if row.get("n_votes") == 0]
    assert len(unstarted) == summary["gold_unstarted"]


def test_the_summary_says_up_front_whether_the_budget_covers_the_draw(
    lane, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="gold", n=8, max_usd=0.020, workers=1)
    assert summary["est_cost_usd_for_draw"] == pytest.approx(
        round(summary["drawn"] * judge_lane.min_budget_usd("gold"), 4)
    )
    assert summary["budget_covers_draw"] is False
    rich = lane(tmp_path / "out2", export_run="1", tier="text", n=8, max_usd=5, workers=1)
    assert rich["budget_covers_draw"] is True
