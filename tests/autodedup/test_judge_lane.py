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
import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image as PILImage

from autodedup import harness, judge, judge_lane
from autodedup.judge_sql import (
    GOLD_PAIRS_SQL,
    JUDGEMENT_CACHED_SQL,
    JUDGEMENT_COST_SQL,
    JUDGEMENT_POD_COST_SQL,
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
    def __init__(self, tool_calls: list[dict[str, Any]], cost_usd: float, call_id: int,
                 duration_ms: int = 0) -> None:
        self.tool_calls = tool_calls
        self.cost_usd = cost_usd
        self.llm_call_id = call_id
        self.duration_ms = duration_ms
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
                 transient_fails: dict[str, int] | None = None,
                 duration_ms: int = 0) -> None:
        self.calls = calls
        self.cost = cost
        self.fail_models = fail_models
        self.fail_all = fail_all
        self.error = error
        self.transient_fails = dict(transient_fails or {})
        self.duration_ms = duration_ms

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
            self.duration_ms,
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
        elif sql is GOLD_PAIRS_SQL:
            self._rows = list(self._conn.gold)
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
                 cost_row: tuple[Any, Any], upserts_fail: bool = False,
                 gold: list[tuple[int, int]] | None = None) -> None:
        self.executed = executed
        self.cached = cached
        self.gold = list(gold or [])
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
        "gold": [],
        "pod": SimpleNamespace(
            base_url="https://pod-1-8000.proxy.runpod.net",
            pod_id="pod-1",
            gpu="NVIDIA L4",
            usd_per_hr=0.44,
            started_at=0.0,   # restamped at launch, exactly as PodHandle is
            model_id=judge_lane.MODEL_OSS,
            dry_run=False,
        ),
        "pod_starts": [],
        "pod_stops": [],
        "receipt_dirs": [],
        "oss_base_urls": [],
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

    def fake_pod_start(model: str, gpu: str | None, out_dir: Path | None = None) -> Any:
        state["pod_starts"].append((model, gpu))
        state["receipt_dirs"].append(out_dir)
        pod = state["pod"]
        pod.started_at = judge_lane.CLOCK()   # as `launch_vllm_pod` stamps a real handle
        return pod

    def fake_oss_client(conn: Any) -> Any:
        # Recorded AT CONSTRUCTION: the provider reads its endpoint once, so a base URL set
        # after this point would leave every worker talking to nothing.
        state["oss_base_urls"].append(os.environ.get(judge_lane.OSS_BASE_URL_ENV))
        return state["client"]

    monkeypatch.setenv(judge_lane.OSS_BASE_URL_ENV, "")
    monkeypatch.setattr(judge_lane, "pod_start", fake_pod_start)
    monkeypatch.setattr(
        judge_lane, "pod_stop",
        lambda pod, out_dir=None: state["pod_stops"].append(pod),
    )
    monkeypatch.setattr(judge_lane, "oss_llm_client", fake_oss_client)

    def factory() -> FakeConn:
        return FakeConn(executed, state["cached"], state["cost_row"],
                        bool(state.get("upserts_fail")), state["gold"])

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

    def exploding(judge_mod, job, la, lb, settings=None):
        seen.append(job.lo)
        if len(seen) == 1:
            raise ValueError("digest blew up")
        return real(judge_mod, job, la, lb, settings)

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


# --- the pair facts the feature vector cannot carry -----------------------------------------


def _pair_job(feats: dict[str, Any]) -> Any:
    return judge_lane.PairJob(
        lo=1,
        hi=2,
        row={"lo": 1, "hi": 2, "block": "vysocany", "probes": ["K1"], "families": ["ATTR"],
             "feats": {name: list(entry) for name, entry in feats.items()}},
        stratum="mid",
        votes=judge_lane.vote_plan("text"),
    )


def test_the_named_attribute_conflicts_are_exactly_what_the_feature_counted() -> None:
    """The prompt may not argue with the engine: the names come from the SAME slots and the same
    equality the `attr_contradictions` count runs on, so one contradiction is never two lines."""
    from tests.autodedup.test_features import StubFingerprint, compute
    from tests.autodedup.test_features import listing as ft_listing

    shared = {"building_type": "cihlová", "condition": "velmi dobrý"}
    la = ft_listing(1, attrs={**shared, "energy_rating": "B", "ownership": "osobní"})
    lb = ft_listing(2, attrs={**shared, "energy_rating": "C", "ownership": "družstevní"})
    conflicts = judge_lane.attribute_conflicts(la, lb)
    feats = compute(StubFingerprint(1), StubFingerprint(2), la, lb)

    assert [name for name, _, _ in conflicts] == ["energy_rating", "ownership"]
    assert conflicts[0][1:] == ("B", "C")
    assert float(len(conflicts)) == feats["attr_contradictions"][0]

    _, evidence = judge_lane._inputs(judge, _pair_job(dict(feats)), la, lb)
    assert (
        "- contradicting attributes: energy_rating A=B vs B=C;"
        " ownership A=osobní vs B=družstevní" in evidence
    )


def test_the_lane_measures_the_metres_only_when_both_pins_are_street_grain() -> None:
    from autodedup import dataset as ds
    from tests.autodedup.test_features import listing as ft_listing

    fine = ds.Location(
        lat=50.1075, lon=14.4880, granularity="address_point", granularity_rank=100,
        uncertainty_radius_m=10.0,
    )
    near = ds.Location(
        lat=50.1075, lon=14.4938, granularity="street", granularity_rank=60,
        uncertainty_radius_m=50.0,
    )
    coarse = ds.Location(
        lat=50.0900, lon=14.4200, granularity="obec", granularity_rank=40,
    )
    la = ft_listing(1, location=fine)
    lb = ft_listing(2, location=near)
    metres = judge_lane.pin_distance_m(la, lb)
    assert metres is not None and 400.0 < metres < 425.0

    _, evidence = judge_lane._inputs(judge, _pair_job({"dist_norm": (0.32, True)}), la, lb)
    assert re.search(r"- distance: 41\d m \(pin radii 10 m \+ 50 m\)", evidence)

    lc = ft_listing(3, location=coarse)
    assert judge_lane.pin_distance_m(la, lc) is None
    _, coarse_evidence = judge_lane._inputs(
        judge, _pair_job({"dist_norm": (0.0, False)}), la, lc
    )
    assert "distance not comparable" in coarse_evidence
    assert "(a municipality-grade pin on side B)" in coarse_evidence
    assert "- distance:" not in coarse_evidence


# --- the oss tier: a model rented by the hour, judged on the SAME pairs ----------------------


def _pairs_of(out: Path) -> list[tuple[int, int]]:
    sample = json.loads((out / judge_lane.SAMPLE_FILE).read_text(encoding="utf-8"))
    return [(int(row["lo"]), int(row["hi"])) for row in sample["pairs"]]


def _ticking(step: float = 1.0):
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


def test_oss_draws_exactly_the_pairs_the_vision_tier_would(lane, tmp_path: Path) -> None:
    """Same seed, same draw — the comparison is worthless if the two arms answer different
    questions, and a differently-seeded sample is a different question."""
    vision = tmp_path / "vision"
    oss = tmp_path / "oss"
    lane(vision, export_run="1", tier="vision", n=6, max_usd=5, workers=1)
    lane(oss, export_run="1", tier="oss", n=6, max_usd=5, workers=1)
    assert _pairs_of(vision) == _pairs_of(oss)


def test_oss_sends_the_vision_prompt_to_the_pod_model(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="oss", n=4, max_usd=5, workers=1)
    assert lane.state["pod_starts"] == [(judge_lane.MODEL_OSS, None)]
    assert lane.calls
    for call in lane.calls:
        # The NAMESPACED id, which is what keeps `oss:Qwen/...` from routing to Alibaba's
        # paid API on the vendor prefix, plus the VISION called_for and real images.
        assert call["model"] == f"{judge_lane.OSS_PREFIX}{judge_lane.MODEL_OSS}"
        assert call["provider"] == judge_lane.OSS_PROVIDER
        assert call["called_for"] == judge_lane.CALLED_FOR["vision"]
    assert lane.state["r2"].reads
    assert summary["oss_model"] == judge_lane.MODEL_OSS
    stored = {row["model"] for row in _upserts(lane.executed)}
    assert stored == {f"{judge_lane.OSS_PREFIX}{judge_lane.MODEL_OSS}"}
    assert {row["tier"] for row in _upserts(lane.executed)} == {"oss"}


def test_the_prompt_is_the_vision_prompt_to_the_character(lane, tmp_path: Path) -> None:
    lane(tmp_path / "vision", export_run="1", tier="vision", n=3, max_usd=5, workers=1)
    vision_messages = [call["messages"] for call in lane.calls]
    lane.calls.clear()
    lane(tmp_path / "oss", export_run="1", tier="oss", n=3, max_usd=5, workers=1)
    assert [call["messages"] for call in lane.calls] == vision_messages


def test_the_base_url_is_set_before_a_client_exists(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1", tier="oss", n=2, max_usd=5, workers=1)
    assert lane.state["oss_base_urls"] == [f"{lane.state['pod'].base_url}/v1"]


@pytest.mark.parametrize("given, wanted", [
    ("https://pod-1-8000.proxy.runpod.net", "https://pod-1-8000.proxy.runpod.net/v1"),
    ("https://pod-1-8000.proxy.runpod.net/", "https://pod-1-8000.proxy.runpod.net/v1"),
    ("https://pod-1-8000.proxy.runpod.net/v1", "https://pod-1-8000.proxy.runpod.net/v1"),
])
def test_the_pod_root_becomes_the_openai_api_root(given: str, wanted: str) -> None:
    """The handle carries the HTTP root (`oss_pod.wait_ready` polls `/v1/models` off it); an
    OpenAI-compatible client posts to `{base}/chat/completions`. Whoever adds the suffix, the
    lane publishes the same URL."""
    assert judge_lane.openai_base_url(given) == wanted


def test_the_pod_bill_is_divided_over_what_it_produced(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(judge_lane, "CLOCK", _ticking(60.0))
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="oss", n=3, max_usd=5, workers=1)

    assert summary["spent_usd_source"] == "pod"
    assert summary["pod_id"] == "pod-1" and summary["gpu"] == "NVIDIA L4"
    assert summary["usd_per_hr"] == 0.44 and summary["pod_hours"] > 0
    assert summary["pod_cost_usd"] == pytest.approx(summary["pod_hours"] * 0.44, abs=1e-5)
    assert summary["spent_usd"] == pytest.approx(summary["pod_cost_usd"], abs=1e-6)
    share = summary["cost_per_pair_usd"]
    assert share == pytest.approx(summary["pod_cost_usd"] / summary["done"], abs=1e-6)
    assert summary["pod_wall_s_per_pair"] and summary["latency_s_mean"] is not None
    # Two different clocks, and the names have to keep them apart: `pod_wall_s_per_pair` is
    # the rental over the pairs, `latency_s_mean` is one call's duration — the one that
    # `oss_s_per_pair` is expressed in. A summary key spelled `s_per_pair` invites the operator
    # to feed the wrong one back and cap the next pass `workers` times too low.
    assert "s_per_pair" not in summary

    # NULL while the pod runs (unknown, not free), settled once it is gone — in the store
    # and in the artifact the comparison reads.
    assert [row["cost_usd"] for row in _upserts(lane.executed)] == [None] * summary["done"]
    settles = [params for sql, params in lane.executed if sql is JUDGEMENT_POD_COST_SQL]
    assert len(settles) == 1
    assert settles[0]["cost_usd"] == pytest.approx(share, abs=1e-6)
    assert settles[0]["tier"] == "oss"
    assert len(settles[0]["los"]) == summary["done"]
    assert all(row["cost_usd"] == pytest.approx(share, abs=1e-6)
               for row in _judgements(out))


def test_the_preflight_estimate_counts_the_boot_it_has_already_paid_for(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bill starts at LAUNCH: by the time the estimate is computed, `pod_start` has waited
    out a weights load that routinely runs longer than the judging window itself. A look-ahead
    counting only the pairs publishes a fraction of the bill `pod_cost_usd` later prices."""
    boot_s = 600.0
    real_start = judge_lane.pod_start

    def slow_boot(model: str, gpu: str | None, out_dir: Path | None = None) -> Any:
        pod = real_start(model, gpu, out_dir)
        pod.started_at -= boot_s   # as if the launch stamp were ten minutes old
        return pod

    monkeypatch.setattr(judge_lane, "pod_start", slow_boot)
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="oss", n=2, max_usd=20, workers=1,
                   oss_s_per_pair=10)
    pairs_only = judge_lane.oss_est_usd(0.44, summary["drawn"], 1, 10.0)
    assert summary["pod_boot_s"] == pytest.approx(boot_s, abs=5.0)
    assert summary["est_cost_usd_for_draw"] > pairs_only
    assert summary["est_cost_usd_for_draw"] == pytest.approx(
        pairs_only + 0.44 * boot_s / 3600.0, abs=1e-3
    )


def test_the_budget_margin_is_one_whole_call_not_a_workers_share(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six workers judging in parallel each occupy `s_per_pair` of WALL clock, so admitting one
    more pair costs a whole call's duration — a margin of a sixth of it lets the pod run past
    `max_usd`, and a cap the run can exceed is a number nobody trusts twice."""
    seen: list[float] = []
    real = judge_lane.PodBudget

    def record(*args: Any, **kwargs: Any) -> Any:
        seen.append(float(kwargs["margin_s"]))
        return real(*args, **kwargs)

    monkeypatch.setattr(judge_lane, "PodBudget", record)
    lane(tmp_path / "out", export_run="1", tier="oss", n=2, max_usd=20, workers=6,
         oss_s_per_pair=18)
    assert seen == [18.0]


def test_a_pod_that_never_boots_still_writes_the_summary(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one path that can burn 25 minutes of rental and produce no verdict. `_rails`' rule
    holds here too: the artifact is the evidence, so it lands BEFORE the exception."""
    def no_capacity(model: str, gpu: str | None, out_dir: Path | None = None) -> Any:
        raise RuntimeError("no capacity for any eligible gpu")

    monkeypatch.setattr(judge_lane, "pod_start", no_capacity)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError):
        lane(out, export_run="1", tier="oss", n=2, max_usd=5, workers=1)
    summary = json.loads((out / judge_lane.SUMMARY_FILE).read_text(encoding="utf-8"))
    assert "no capacity" in summary["pod_boot_failed"]
    assert summary["pod_boot_s"] >= 0 and summary["done"] == 0
    assert summary["spent_usd_source"].startswith("pod (boot failed")


def test_the_pod_is_terminated_once_on_a_clean_pass(lane, tmp_path: Path) -> None:
    lane(tmp_path / "out", export_run="1", tier="oss", n=2, max_usd=5, workers=1)
    assert lane.state["pod_stops"] == [lane.state["pod"]]


def test_the_pod_is_terminated_when_the_pass_dies(
    lane, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the judge loop fell over")

    monkeypatch.setattr(judge_lane, "_dispatch", explode)
    with pytest.raises(RuntimeError):
        lane(tmp_path / "out", export_run="1", tier="oss", n=2, max_usd=5, workers=1)
    assert lane.state["pod_stops"] == [lane.state["pod"]]


def test_no_pairs_left_to_judge_rents_no_pod(lane, tmp_path: Path) -> None:
    out = tmp_path / "out"
    lane(out, export_run="1", tier="oss", n=3, max_usd=5, workers=1)
    lane.state["cached"] = [(row["lo"], row["hi"]) for row in _judgements(out)]
    lane.state["pod_starts"].clear()
    lane.state["pod_stops"].clear()
    again = lane(tmp_path / "out2", export_run="1", tier="oss", n=3, max_usd=5, workers=1)
    assert again["skipped_cached"] == again["drawn"]
    assert lane.state["pod_starts"] == [] and lane.state["pod_stops"] == []


def test_the_pod_budget_stops_on_the_clock_not_on_the_call_count() -> None:
    # $1 a second, so the numbers read straight off the clock.
    clock = iter([0.0, 0.5, 1.5])
    budget = judge_lane.PodBudget(
        2.0, usd_per_hr=3600.0, margin_s=1.0, started=0.0, clock=lambda: next(clock)
    )
    assert budget.reserve(0.0) is True          # 0 s spent + 1 s margin = $1.00, fits
    assert budget.spent == pytest.approx(0.5, rel=1e-3)
    assert budget.reserve(0.0) is False         # 1.5 s spent + 1 s margin = $2.50, does not
    assert budget.stopped is True


def test_a_budget_that_cannot_pay_for_a_pod_is_refused(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1", tier="oss", n=2,
             max_usd=judge_lane.OSS_MIN_USD / 2)
    assert "oss" in str(exc.value)
    assert lane.state["pod_starts"] == []


def test_pairs_from_gold_restricts_the_draw_to_pairs_with_ground_truth(
    lane, tmp_path: Path
) -> None:
    whole = tmp_path / "whole"
    lane(whole, export_run="1", tier="oss", n=8, max_usd=5, workers=1)
    every = _pairs_of(whole)
    assert len(every) > 2
    lane.state["gold"] = every[:2]

    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="oss", n=8, max_usd=5, workers=1,
                   pairs_from="gold")
    assert summary["pairs_from"] == "gold"
    assert summary["pairs_without_gold_dropped"] > 0
    assert set(_pairs_of(out)) <= set(every[:2])


def test_pairs_from_gold_refuses_when_no_pair_has_a_gold_row(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        lane(tmp_path / "out", export_run="1", tier="oss", n=4, max_usd=5,
             pairs_from="gold")
    assert "gold" in str(exc.value)
    assert lane.state["pod_starts"] == []


def test_pairs_from_only_accepts_gold(lane, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        lane(tmp_path / "out", export_run="1", tier="oss", n=4, max_usd=5,
             pairs_from="vision")


# --- the adapter onto autodedup.oss_pod -----------------------------------------------------


class FakeRunPod:
    """Only what `pod_stop` needs: `terminate_pod` is idempotent on a live client (404 is
    success) and raises when the pod is still rented."""

    def __init__(self) -> None:
        self.terminated: list[str] = []
        self.fail_terminate = False

    def terminate_pod(self, pod_id: str) -> None:
        self.terminated.append(pod_id)
        if self.fail_terminate:
            raise RuntimeError("pod terminate failed (503)")


class FakePodModule:
    """`autodedup.oss_pod`'s four entry points, recorded. Pinning the CALL SHAPE here is the
    point: the lane and the pod module are written apart, and a renamed keyword would only
    surface against a real RunPod bill."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on = fail_on
        self.client = FakeRunPod()
        self.handle = SimpleNamespace(pod_id="pod-9", base_url="https://pod-9.example",
                                      gpu="L4", usd_per_hr=0.4, started_at=1.0)

    def _record(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")

    def launch_vllm_pod(self, client: Any, **kwargs: Any) -> Any:
        self._record("launch", client=client, **kwargs)
        return self.handle

    def wait_ready(self, handle: Any, **kwargs: Any) -> float:
        self._record("wait_ready", handle=handle, **kwargs)
        return 1.0

    def smoke_chat(self, handle: Any, **kwargs: Any) -> dict[str, Any]:
        self._record("smoke_chat", handle=handle, **kwargs)
        return {}

    def terminate(self, handle: Any, **kwargs: Any) -> None:
        self.calls.append(("terminate", {"handle": handle, **kwargs}))


@pytest.fixture()
def pod_module(monkeypatch: pytest.MonkeyPatch) -> FakePodModule:
    module = FakePodModule()
    monkeypatch.setattr(judge_lane, "pod_module", lambda: module)
    monkeypatch.setattr(judge_lane, "runpod_client", lambda: module.client)
    return module


def test_pod_start_launches_waits_and_smokes_before_a_pair_is_judged(
    pod_module: FakePodModule,
) -> None:
    handle = judge_lane.pod_start(judge_lane.MODEL_OSS, None)
    assert handle is pod_module.handle
    assert [name for name, _ in pod_module.calls] == ["launch", "wait_ready", "smoke_chat"]
    assert pod_module.calls[0][1]["model_id"] == judge_lane.MODEL_OSS
    assert "gpu_preference" not in pod_module.calls[0][1]
    assert pod_module.calls[1][1]["client"] is pod_module.client


def test_a_named_gpu_becomes_the_first_preference(pod_module: FakePodModule) -> None:
    judge_lane.pod_start(judge_lane.MODEL_OSS, "NVIDIA L40S")
    assert pod_module.calls[0][1]["gpu_preference"] == ("NVIDIA L40S",)


def test_a_named_gpu_is_a_pin_not_a_ranking(pod_module: FakePodModule) -> None:
    """`select_gpus` ranks by default and only FILTERS under `strict`. An operator names a
    type for its price, so a silent fallback to the widened rung is the one substitution this
    arm cannot afford; unpinned, the default preference must stay a ranking."""
    judge_lane.pod_start(judge_lane.MODEL_OSS, "NVIDIA RTX A5000")
    assert pod_module.calls[0][1]["strict_gpu"] is True
    pod_module.calls.clear()
    judge_lane.pod_start(judge_lane.MODEL_OSS, None)
    assert "strict_gpu" not in pod_module.calls[0][1]


def test_a_cancelled_boot_still_terminates_the_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Ctrl-C / Actions cancellation inside the smoke window is a BaseException, so an
    `except Exception` here would let it past BOTH guards — `pod_start` has not returned, so
    the lane's `finally` does not know the pod exists — and it would bill until noticed."""
    module = FakePodModule()

    def cancelled(handle: Any, **kwargs: Any) -> dict[str, Any]:
        module.calls.append(("smoke_chat", {"handle": handle}))
        raise KeyboardInterrupt

    module.smoke_chat = cancelled  # type: ignore[method-assign]
    monkeypatch.setattr(judge_lane, "pod_module", lambda: module)
    monkeypatch.setattr(judge_lane, "runpod_client", lambda: module.client)
    with pytest.raises(KeyboardInterrupt):
        judge_lane.pod_start(judge_lane.MODEL_OSS, None)
    assert pod_module_terminated(module) == [module.handle]


@pytest.mark.parametrize("step", ["wait_ready", "smoke_chat"])
def test_a_pod_that_never_served_is_torn_down_on_the_way_out(
    monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """The lane's own `finally` only knows about a pod `pod_start` RETURNED, so a rental that
    dies before that has to clean up after itself or it bills until someone notices."""
    module = FakePodModule(fail_on=step)
    monkeypatch.setattr(judge_lane, "pod_module", lambda: module)
    monkeypatch.setattr(judge_lane, "runpod_client", lambda: module.client)
    with pytest.raises(RuntimeError):
        judge_lane.pod_start(judge_lane.MODEL_OSS, None)
    assert pod_module_terminated(module) == [module.handle]


def pod_module_terminated(module: FakePodModule) -> list[Any]:
    return [kwargs["handle"] for name, kwargs in module.calls if name == "terminate"]


def test_pod_stop_hands_the_pod_module_a_client(pod_module: FakePodModule) -> None:
    judge_lane.pod_stop(pod_module.handle)
    assert pod_module.calls == [("terminate", {"handle": pod_module.handle,
                                               "client": pod_module.client})]


def test_pod_stop_verifies_the_pod_is_actually_gone(pod_module: FakePodModule) -> None:
    """`oss_pod.terminate` never raises, so on its own it can report a leak only to the log.
    The second (idempotent) delete is what makes the teardown a signal the lane can read."""
    judge_lane.pod_stop(pod_module.handle)
    assert pod_module.client.terminated == ["pod-9"]


def test_a_pod_that_would_not_die_raises_out_of_pod_stop(
    pod_module: FakePodModule,
) -> None:
    pod_module.client.fail_terminate = True
    with pytest.raises(RuntimeError):
        judge_lane.pod_stop(pod_module.handle)


def test_the_real_pod_module_still_exposes_what_the_lane_calls() -> None:
    from autodedup import oss_pod

    for name in ("launch_vllm_pod", "wait_ready", "smoke_chat", "terminate"):
        assert callable(getattr(oss_pod, name)), name
    for field in ("pod_id", "base_url", "gpu", "usd_per_hr", "started_at"):
        assert field in oss_pod.PodHandle.__dataclass_fields__, field


def test_the_lane_and_the_provider_agree_on_the_oss_namespace() -> None:
    """The lane restates the provider's prefix and name; a drift in either is discovered
    AFTER a GPU has been rented (`served_model()` stops stripping, and the wire id
    `oss/Qwen/...` is unknown to vLLM, so every pair fails at full price)."""
    from api.providers import oss as oss_provider

    assert judge_lane.OSS_PREFIX == oss_provider.MODEL_PREFIX
    assert judge_lane.OSS_PROVIDER == oss_provider.OssProvider.name


def test_latency_is_the_call_not_the_backoff(lane, tmp_path: Path) -> None:
    """`_call_with_retry` sleeps out a 429 — the steady state at six workers on the paid tiers
    and unheard of on a rented pod with no rate limit. Billing that sleep to `latency_s` would
    bias the very yardstick the oss arm is measured against, so the reported number is the
    provider's own `duration_ms`; the wall span stays beside it as `pair_wall_s`."""
    lane.state["client"].duration_ms = 4200
    out = tmp_path / "out"
    summary = lane(out, export_run="1", tier="text", n=2, max_usd=5, workers=1)
    rows = _judgements(out)
    assert rows and all(row["latency_s"] == pytest.approx(4.2) for row in rows)
    # The fake provider returns instantly, so the wall span cannot reach the claimed duration:
    # the two numbers are measuring different things, which is the point.
    assert all(row["pair_wall_s"] < 4.2 for row in rows)
    assert summary["latency_s_mean"] == pytest.approx(4.2)


def test_the_experimental_arm_never_becomes_a_pairs_headline_verdict() -> None:
    """Migration 530 admits a fourth tier into a table two consumers read WITHOUT filtering
    tier, and the oss arm answers exactly the pairs gold already answered — on `created_at`
    alone the rented 7B would replace ground truth as the review queue's headline and count
    itself into `n_judged_edges`. Authority, not recency."""
    from autodedup import score_sql, ui_sql

    headline = ui_sql.RESIDUAL_SQL
    assert "WHEN 'gold' THEN 0" in headline
    assert headline.index("WHEN 'gold' THEN 0") < headline.index("ELSE 3 END")
    assert "tier <> 'oss'" in score_sql.JUDGED_EDGES_SQL


def test_the_oss_lane_gets_every_credential_its_pod_needs() -> None:
    """A secret that is missing from the workflow env fails LATE and expensively: a gated
    `oss_model` 401s on the weights inside the container, never binds, and is caught only by
    `wait_ready`'s 25-minute deadline — a whole rental for a value that was sitting in the
    repository's secrets. Cheap to assert here, not cheap to discover there."""
    from pathlib import Path

    body = (Path(__file__).resolve().parents[2] / ".github" / "workflows"
            / "autodedup.yml").read_text(encoding="utf-8")
    for secret in ("RUNPOD_API_KEY", "HF_TOKEN"):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in body, secret


def test_the_lane_leaves_a_receipt_from_launch_until_the_pod_is_confirmed_gone(
    tmp_path: Path, pod_module: FakePodModule,
) -> None:
    """The receipt has to exist across the whole rental — it is written BEFORE the readiness
    wait, which is both the longest window and the likeliest one to be cancelled in — and it
    has to be gone after the verifying DELETE, or the reaper cries leak on every clean run."""
    written: list[Path] = []
    cleared: list[Path] = []
    pod_module.write_receipt = (  # type: ignore[method-assign]
        lambda handle, out_dir: written.append(Path(out_dir)) or Path(out_dir) / "pod.json"
    )
    pod_module.clear_receipt = lambda out_dir: cleared.append(Path(out_dir))  # type: ignore[method-assign]

    judge_lane.pod_start(judge_lane.MODEL_OSS, None, tmp_path)
    assert written == [tmp_path]
    # Written before the wait, not after: the order of the recorded calls is the assertion.
    assert [name for name, _ in pod_module.calls] == ["launch", "wait_ready", "smoke_chat"]
    assert cleared == []

    judge_lane.pod_stop(pod_module.handle, tmp_path)
    assert cleared == [tmp_path]


def test_the_real_pod_module_exposes_the_receipt_the_lane_writes() -> None:
    from autodedup import oss_pod

    for name in ("write_receipt", "clear_receipt", "reap_receipt"):
        assert callable(getattr(oss_pod, name)), name


# --- one settings row for the engine and the prompt (W4) -----------------------------------


def test_the_lane_takes_one_settings_row_for_the_engine_and_the_prompt() -> None:
    """The row that scores the cohort is the row the digest is built through (`--args settings=`).

    Two rows would let the prompt name a contradiction on a slot the engine refuses to count."""
    import inspect

    parsed = judge_lane.parse_args(
        {"export_run": "1", "tier": "text", "n": "4", "max_usd": "5", "settings": "s.json"}
    )
    assert parsed.settings == "s.json"
    assert judge_lane.parse_args(
        {"export_run": "1", "tier": "text", "n": "4", "max_usd": "5"}
    ).settings is None
    for name in ("_inputs", "_estimate", "_run_job", "_dispatch"):
        assert "settings" in inspect.signature(getattr(judge_lane, name)).parameters, name


def test_the_lane_scores_the_cohort_with_the_named_model(tmp_path, monkeypatch) -> None:
    """`--args model=` picks a fitted model under autodedup/models/; absent = the hand prior."""
    from autodedup import score_lane
    from autodedup.model import hand_initialised

    parsed = judge_lane.parse_args(
        {"export_run": "1", "tier": "text", "n": "4", "max_usd": "5", "model": "w4_gold"}
    )
    assert parsed.model == "w4_gold"
    assert judge_lane.parse_args(
        {"export_run": "1", "tier": "text", "n": "4", "max_usd": "5"}
    ).model is None
    assert judge_lane.load_engine_model(None).feature_order == hand_initialised().feature_order
    (tmp_path / "m.json").write_text(json.dumps(hand_initialised().to_json()), encoding="utf-8")
    monkeypatch.setattr(score_lane, "MODELS_DIR", tmp_path)
    loaded = judge_lane.load_engine_model("m")
    assert loaded.feature_order == hand_initialised().feature_order
