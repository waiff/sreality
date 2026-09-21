"""`mode=facts`: argument gates, the advert read, the pass, and what the summary promises."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autodedup import fact_cards, fact_cards_lane, lane
from autodedup.export_sql import COHORT_LISTINGS_SQL
from autodedup.judge_sql import JUDGEMENT_COST_SQL

CARD_INPUT: dict[str, Any] = {
    "unit_code": "B1.2.1",
    "floor_raw": "2. NP",
    "floor": 1,
    "total_floors": None,
    "area_m2": 74.0,
    "other_areas": [{"label": "terasa", "m2": 7.9}],
    "parcel_numbers": [],
    "plot_number": None,
    "street": "Litovelská",
    "house_number": "101/17",
    "locality": "Olomouc",
    "project_name": None,
    "building_block": None,
    "disposition": "2+kk",
    "rooms_offered": None,
    "capacity_persons": None,
    "accessories": [],
    "orientation": None,
    "is_menu": False,
    "menu_units": [],
    "is_new_development": True,
    "evidence": {"unit_code": "byt B1.2.1"},
}

LISTING_COLUMNS: tuple[str, ...] = (
    "id", "source", "source_id_native", "source_url", "category_main", "category_type",
    "subtype", "disposition", "area_m2", "floor", "total_floors", "price_czk", "price_unit",
    "area_basis", "has_balcony", "has_parking", "has_lift", "building_type", "condition",
    "energy_rating", "estate_area", "usable_area", "garden_area", "category_sub_cb",
    "furnished", "terrace", "cellar", "garage", "parking_lots", "ownership", "published_at",
    "description", "first_seen_at", "last_seen_at", "inactive_at", "is_active",
    "broker_identity_id", "broker_firm_id", "broker_phone", "broker_email",
)


class FakeResponse:
    def __init__(self, tool_calls: list[dict[str, Any]], cost_usd: float, call_id: int) -> None:
        self.tool_calls = tool_calls
        self.cost_usd = cost_usd
        self.llm_call_id = call_id
        self.input_tokens = 900
        self.output_tokens = 120
        self.model = "fake-model"
        self.text = ""


class FakeLLM:
    def __init__(self, calls: list[dict[str, Any]], cost: float = 0.0004,
                 fail_all: bool = False, no_tool: bool = False,
                 error: str = "provider says no") -> None:
        self.calls = calls
        self.cost = cost
        self.fail_all = fail_all
        self.no_tool = no_tool
        self.error = error

    def call(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.fail_all:
            raise RuntimeError(self.error)
        if self.no_tool:
            return FakeResponse([], self.cost, len(self.calls))
        return FakeResponse(
            [{"id": "t1", "name": fact_cards.TOOL_NAME, "input": dict(CARD_INPUT)}],
            self.cost, len(self.calls),
        )


class FakeCursor:
    def __init__(self, conn: "FakeConn") -> None:
        self._conn = conn
        self._rows: list[tuple[Any, ...]] = []
        self.description: list[tuple[str, ...]] | None = None

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))
        if sql is COHORT_LISTINGS_SQL:
            wanted = set(params["ids"])
            self.description = [(name,) for name in LISTING_COLUMNS]
            self._rows = [
                self._conn.rows[listing_id]
                for listing_id in sorted(wanted & set(self._conn.rows))
            ]
        elif sql is JUDGEMENT_COST_SQL:
            self._rows = [self._conn.cost_row]
        else:
            self._rows = []

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows: dict[int, tuple[Any, ...]], executed: list[tuple[str, Any]],
                 cost_row: tuple[Any, Any] = (0.0032, 8)) -> None:
        self.rows = rows
        self.executed = executed
        self.cost_row = cost_row
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def close(self) -> None:
        self.closed = True


def listing_row(listing_id: int, description: str | None, source: str = "sreality"
                ) -> tuple[Any, ...]:
    values: dict[str, Any] = {name: None for name in LISTING_COLUMNS}
    values.update({
        "id": listing_id, "source": source, "description": description,
        "category_main": "byt", "category_type": "prodej", "area_m2": 74.0,
        "broker_phone": "777123456", "broker_email": "jan@example.cz",
    })
    return tuple(values[name] for name in LISTING_COLUMNS)


@pytest.fixture()
def listings_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "listings"
    directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fact_cards_lane, "LISTINGS_DIR", directory)
    return directory


# --- the argument gates ---------------------------------------------------------------------


def test_the_mode_is_registered_and_carries_a_ledger_row() -> None:
    assert lane.MODES["facts"] is fact_cards_lane.run_facts
    assert lane.ITERATION_META["facts"]["wave"] == "W14"


def test_listings_is_required() -> None:
    with pytest.raises(SystemExit, match="listings"):
        fact_cards_lane.parse_args({"max_usd": "2"})


def test_max_usd_is_required_on_every_paid_lane() -> None:
    with pytest.raises(SystemExit, match="max_usd is required"):
        fact_cards_lane.parse_args({"listings": "x"})


@pytest.mark.parametrize("value", ["0", "-1", "nope", "26"])
def test_a_budget_that_cannot_bind_refuses_to_start(value: str) -> None:
    with pytest.raises(SystemExit):
        fact_cards_lane.parse_args({"listings": "x", "max_usd": value})


def test_an_unknown_arm_names_the_ones_that_exist() -> None:
    with pytest.raises(SystemExit, match="unknown arm"):
        fact_cards_lane.parse_args({"listings": "x", "max_usd": "2", "arm": "gpt-9"})


def test_each_arm_defaults_to_its_own_worker_count() -> None:
    qwen = fact_cards_lane.parse_args({"listings": "x", "max_usd": "2", "arm": "qwen-flash"})
    assert qwen.workers == 1 and qwen.arm.min_interval_ms > 0
    nano = fact_cards_lane.parse_args({"listings": "x", "max_usd": "2", "arm": "nano"})
    assert nano.workers > 1


def test_overriding_the_model_clears_that_arms_price_rather_than_quoting_the_wrong_one() -> None:
    parsed = fact_cards_lane.parse_args(
        {"listings": "x", "max_usd": "2", "arm": "nano", "model": "gpt-9-turbo"}
    )
    assert parsed.arm.model == "gpt-9-turbo"
    assert parsed.arm.usd_in_per_mtok == 0.0
    assert fact_cards_lane.est_call_usd(parsed.arm, 1200) == 0.0


def test_every_arm_names_a_model_its_provider_actually_prices() -> None:
    from api.providers.openai import PRICES as OPENAI_PRICES
    from api.providers.qwen import PRICES as QWEN_PRICES

    prices = {"openai": OPENAI_PRICES, "qwen": QWEN_PRICES}
    for arm in fact_cards_lane.ARMS.values():
        assert arm.model in prices[arm.provider], arm.name
        priced = prices[arm.provider][arm.model]
        assert arm.usd_in_per_mtok == priced.input_per_mtok
        assert arm.usd_out_per_mtok == priced.output_per_mtok


# --- the listing set ---------------------------------------------------------------------


def test_a_listing_list_loads_and_deduplicates_in_order(listings_dir: Path) -> None:
    (listings_dir / "set.json").write_text("[3, 1, 3, 2]", encoding="utf-8")
    assert fact_cards_lane.load_listing_ids("set") == (3, 1, 2)


def test_an_object_carrying_listing_ids_is_accepted(listings_dir: Path) -> None:
    (listings_dir / "set.json").write_text(
        json.dumps({"listing_ids": [5, 6]}), encoding="utf-8"
    )
    assert fact_cards_lane.load_listing_ids("set") == (5, 6)


@pytest.mark.parametrize("payload", ["[]", "{}", "nope", '["x"]', '"1,2"'])
def test_a_malformed_listing_list_refuses_the_run(listings_dir: Path, payload: str) -> None:
    (listings_dir / "bad.json").write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit):
        fact_cards_lane.load_listing_ids("bad")


def test_a_missing_listing_list_refuses_the_run(listings_dir: Path) -> None:
    with pytest.raises(SystemExit):
        fact_cards_lane.load_listing_ids("absent")


def test_every_committed_listing_set_is_a_non_empty_list_of_positive_ids() -> None:
    files = sorted(fact_cards_lane.LISTINGS_DIR.glob("*.json"))
    assert files, "the experiment's listing set is committed, not improvised"
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        ids = payload.get("listing_ids") if isinstance(payload, dict) else payload
        if ids is None:
            continue  # a manifest beside the set, not a set
        assert ids and all(isinstance(i, int) and i > 0 for i in ids), path


def test_the_w14_experiment_set_and_its_manifest_agree() -> None:
    base = fact_cards_lane.LISTINGS_DIR
    ids = json.loads((base / "w14_factcards.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (base / "w14_factcards_manifest.json").read_text(encoding="utf-8")
    )
    assert sorted(ids) == sorted(int(key) for key in manifest["listings"])
    assert len(ids) <= 1500, "the experiment is capped at ~1,500 listings"
    known = set(ids)
    for pair in manifest["pairs"]:
        assert pair["lo"] in known and pair["hi"] in known
        assert pair["expected"] in {"conflict", "no_conflict"}
    for group in manifest["groups"]:
        assert set(group["members"]) <= known


# --- reading the adverts -------------------------------------------------------------------


def test_the_advert_read_uses_the_export_lanes_own_sql_and_drops_every_contact() -> None:
    executed: list[tuple[str, Any]] = []
    conn = FakeConn({1: listing_row(1, "byt B1.2.1")}, executed)
    rows = fact_cards_lane.read_listings(conn, (1,))
    assert executed[0][0] is COHORT_LISTINGS_SQL
    assert rows[1]["description"] == "byt B1.2.1"
    assert "broker_phone" not in rows[1] and "broker_email" not in rows[1]


def test_the_advert_read_batches_its_ids() -> None:
    executed: list[tuple[str, Any]] = []
    rows = {n: listing_row(n, "x") for n in range(1, 1201)}
    conn = FakeConn(rows, executed)
    read = fact_cards_lane.read_listings(conn, tuple(range(1, 1201)))
    assert len(read) == 1200
    assert len([sql for sql, _ in executed if sql is COHORT_LISTINGS_SQL]) == 3


# --- the pass ------------------------------------------------------------------------------


def run(tmp_path: Path, listings_dir: Path, args: dict[str, str],
        rows: dict[int, tuple[Any, ...]], client: FakeLLM,
        cost_row: tuple[Any, Any] = (0.0032, 8)) -> dict[str, Any]:
    (listings_dir / "set.json").write_text(json.dumps(sorted(rows)), encoding="utf-8")
    conns: list[FakeConn] = []

    def factory() -> FakeConn:
        conn = FakeConn(rows, [], cost_row)
        conns.append(conn)
        return conn

    out = tmp_path / "out"
    summary = fact_cards_lane.run_facts(
        factory, {"listings": "set", **args}, out
    )
    summary["_conns"] = conns
    return summary


def read_cards(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "out" / fact_cards_lane.CARDS_FILE
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_clean_pass_writes_one_card_per_listing_and_reports_done(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    rows = {n: listing_row(n, f"byt B1.2.{n} ve 2. NP") for n in (1, 2, 3)}
    summary = run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"}, rows, client)

    assert summary["counts"]["done"] == 3
    assert summary["counts"]["failed"] == 0
    cards = read_cards(tmp_path)
    assert {row["listing_id"] for row in cards} == {1, 2, 3}
    assert all(row["card"]["unit_code"] == "B1.2.1" for row in cards)
    assert all(row["content_key"] for row in cards)
    assert summary["tokens"]["input"] == 2700


def test_every_call_is_a_forced_tool_call_under_the_reused_called_for(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1", "arm": "qwen-flash"},
        {1: listing_row(1, "byt B1.2.1")}, client)

    assert len(calls) == 1
    assert calls[0]["called_for"] == "autodedup_judge_text"
    assert calls[0]["tool_choice"] == fact_cards.TOOL_NAME
    assert calls[0]["tools"] == [fact_cards.TOOL_SCHEMA]
    assert calls[0]["model"] == "qwen3.7-flash"
    assert calls[0]["provider"] == "qwen"
    assert calls[0]["system"] is fact_cards.SYSTEM_PROMPT


def test_no_contact_detail_reaches_the_provider(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    body = "Byt B1.2.1, volejte 777 123 456, e-mail jan.novak@example.cz"
    run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"},
        {1: listing_row(1, body)}, client)

    sent = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert "777 123 456" not in sent and "example.cz" not in sent
    assert "B1.2.1" in sent


def test_a_listing_without_text_costs_nothing_and_is_counted(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    rows = {1: listing_row(1, None), 2: listing_row(2, "byt B1.2.1")}
    summary = run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"}, rows, client)

    assert summary["counts"]["no_text"] == 1 and summary["counts"]["done"] == 1
    assert len(calls) == 1
    assert [row["status"] for row in read_cards(tmp_path) if row["listing_id"] == 1] \
        == ["no_text"]


def test_a_listing_the_database_does_not_hold_is_reported_not_swallowed(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    (listings_dir / "set.json").write_text("[1, 999]", encoding="utf-8")
    rows = {1: listing_row(1, "byt B1.2.1")}
    summary = fact_cards_lane.run_facts(
        lambda: FakeConn(rows, []), {"listings": "set", "max_usd": "1", "workers": "1"},
        tmp_path / "out",
    )
    assert summary["counts"]["not_found"] == 1
    assert summary["counts"]["done"] == 1


def test_the_budget_binds_before_the_call_and_stops_the_pass(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    rows = {n: listing_row(n, "byt B1.2.1 " * 40) for n in range(1, 21)}
    # Two calls' worth of estimate, no more.
    per_call = fact_cards_lane.est_call_usd(fact_cards_lane.ARMS["nano"], 400)
    summary = run(tmp_path, listings_dir,
                  {"max_usd": f"{per_call * 2.5:.6f}", "workers": "1"}, rows, client)

    assert summary["counts"]["skipped_budget"] > 0
    assert summary["counts"]["done"] < 20
    assert len(calls) == summary["counts"]["done"]


def test_a_budget_too_small_for_one_card_refuses_before_anything_runs(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    with pytest.raises(SystemExit, match="cannot pay for a single"):
        run(tmp_path, listings_dir, {"max_usd": "0.0000001", "workers": "1"},
            {1: listing_row(1, "byt B1.2.1")}, client)
    assert calls == []


def test_the_summary_says_whether_the_budget_covers_the_whole_draw(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    summary = run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"},
                  {1: listing_row(1, "byt B1.2.1")}, client)
    assert summary["budget_covers_estimate"] is True
    assert summary["est_cost_usd"] > 0


def test_a_billed_call_that_read_back_nothing_is_recorded_as_unparsed(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls, no_tool=True)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    with pytest.raises(SystemExit):
        run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"},
            {1: listing_row(1, "byt B1.2.1")}, client)
    cards = read_cards(tmp_path)
    assert cards[0]["status"] == "unparsed"
    assert cards[0]["cost_usd"] > 0
    assert cards[0]["llm_call_id"] is not None


def test_a_pass_that_read_nothing_back_exits_non_zero_rather_than_looking_green(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls, fail_all=True, error="401 invalid api key")
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    monkeypatch.setattr(fact_cards_lane, "RETRY_SLEEP", lambda _s: None)
    with pytest.raises(SystemExit, match="0 card"):
        run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"},
            {1: listing_row(1, "byt B1.2.1")}, client)
    # The summary is written BEFORE the raise: a failure nobody can read is a failure twice.
    written = json.loads((tmp_path / "out" / "facts.json").read_text(encoding="utf-8"))
    assert written["counts"]["failed"] == 1 and written["errors"]


def test_an_exhausted_quota_aborts_the_pass_instead_of_paying_for_the_same_error(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(
        calls, fail_all=True,
        error="Error code: 429 - You exceeded your current quota, please check your plan",
    )
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    monkeypatch.setattr(fact_cards_lane, "RETRY_SLEEP", lambda _s: None)
    rows = {n: listing_row(n, "byt B1.2.1") for n in range(1, 11)}
    with pytest.raises(SystemExit):
        run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"}, rows, client)
    written = json.loads((tmp_path / "out" / "facts.json").read_text(encoding="utf-8"))
    assert written["budget_fatal"]
    assert written["counts"]["skipped_fatal"] > 0
    assert len(calls) == 1


def test_a_transient_rate_limit_is_retried_rather_than_shrinking_the_pass(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    class Flaky(FakeLLM):
        def call(self, **kwargs: Any) -> FakeResponse:
            if len(self.calls) == 0:
                self.calls.append(kwargs)
                raise RuntimeError("openai call failed: HTTP 429 slow down")
            return super().call(**kwargs)

    client = Flaky(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    monkeypatch.setattr(fact_cards_lane, "RETRY_SLEEP", lambda _s: None)
    summary = run(tmp_path, listings_dir, {"max_usd": "1", "workers": "1"},
                  {1: listing_row(1, "byt B1.2.1")}, client)
    assert summary["counts"]["done"] == 1
    assert summary["pacer"]["retries"] == 1


def test_the_spend_is_read_from_llm_calls_never_extrapolated(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls, cost=0.5)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    rows = {n: listing_row(n, "byt B1.2.1") for n in (1, 2)}
    summary = run(tmp_path, listings_dir, {"max_usd": "2", "workers": "1"}, rows, client,
                  cost_row=(0.0071, 2))
    assert summary["spent_usd"] == 0.0071
    assert summary["llm_calls"] == 2
    assert summary["usd_per_listing"] == pytest.approx(0.0071 / 2)
    assert summary["est_cost_usd"] != summary["spent_usd"]


def test_the_summary_names_the_arm_the_model_and_the_set_it_ran_over(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    summary = run(tmp_path, listings_dir,
                  {"max_usd": "1", "workers": "1", "arm": "luna"},
                  {1: listing_row(1, "byt B1.2.1")}, client)
    assert summary["arm"] == "luna" and summary["model"] == "gpt-5.6-luna"
    assert summary["listings_file"] == "set"
    assert summary["prompt_version"] == fact_cards.PROMPT_VERSION


def test_limit_narrows_the_pass_without_touching_the_committed_set(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    rows = {n: listing_row(n, "byt B1.2.1") for n in range(1, 11)}
    summary = run(tmp_path, listings_dir,
                  {"max_usd": "1", "workers": "1", "limit": "3"}, rows, client)
    assert summary["counts"]["listings_requested"] == 3
    assert summary["counts"]["done"] == 3


def test_workers_each_open_their_own_connection_and_close_it(
    tmp_path: Path, listings_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    client = FakeLLM(calls)
    monkeypatch.setattr(fact_cards_lane, "llm_client", lambda conn: client)
    rows = {n: listing_row(n, "byt B1.2.1") for n in range(1, 9)}
    summary = run(tmp_path, listings_dir, {"max_usd": "1", "workers": "4"}, rows, client)
    conns = summary.pop("_conns")
    # one for the advert read, four workers, one for the cost read
    assert len(conns) == 6
    assert all(conn.closed for conn in conns)
